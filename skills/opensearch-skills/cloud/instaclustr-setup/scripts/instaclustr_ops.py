#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Instaclustr Provisioning API client for OpenSearch cluster management."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

BASE_URL = "https://api.instaclustr.com/cluster-management/v2"
CREATE_PATH = "/resources/applications/opensearch/clusters/v2/"
HTTP_TIMEOUT_S = 30.0

ALLOWED_CLOUD_PROVIDERS = frozenset(
    {"AWS_VPC", "GCP", "AZURE", "AZURE_AZ", "ONPREMISES"}
)
TERMINAL_CLUSTER_STATUSES = frozenset({"FAILED"})

_PROVIDER_SETTINGS = {
    "awsSettings": {
        "providers": frozenset({"AWS_VPC"}),
        "keys": frozenset(
            {"ebsEncryptionKey", "customVirtualNetworkId", "storageNetwork"}
        ),
    },
    "gcpSettings": {
        "providers": frozenset({"GCP"}),
        "keys": frozenset({"customVirtualNetworkId"}),
    },
    "azureSettings": {
        "providers": frozenset({"AZURE", "AZURE_AZ"}),
        "keys": frozenset(
            {"resourceGroup", "customVirtualNetworkId", "storageNetwork"}
        ),
    },
}


class InstaclustrError(Exception):
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"Instaclustr API error {status}: {body}")


class CredentialError(Exception):
    pass


class InstaclustrTransportError(Exception):
    """Network or TLS failure before an HTTP response is available."""


class UserInputError(ValueError):
    """Invalid user-supplied CLI or create-payload input."""


class UnsafePayloadError(UserInputError):
    """Payload contains secret material that must not be emitted."""


class ClusterOperationError(Exception):
    """The cluster entered a terminal state while waiting."""

    def __init__(
        self, cluster_id: str, status: str | None, operation_status: str | None
    ) -> None:
        self.cluster_id = cluster_id
        self.status = status
        self.operation_status = operation_status
        super().__init__(
            f"Cluster {cluster_id} reached terminal failure: "
            f"status={status}, currentClusterOperationStatus={operation_status}"
        )


def credentials_from_env() -> tuple[str, str]:
    username = os.environ.get("INSTACLUSTR_API_USERNAME")
    api_key = os.environ.get("INSTACLUSTR_API_KEY")
    if not username or not api_key:
        raise CredentialError(
            "Missing Instaclustr credentials. Set INSTACLUSTR_API_USERNAME and "
            "INSTACLUSTR_API_KEY to a Provisioning API key created in the "
            "Instaclustr console (Account Settings → API Keys → Provisioning)."
        )
    return username, api_key


def client_from_env() -> InstaclustrClient:
    """Build an :class:`InstaclustrClient` from environment credentials."""
    username, api_key = credentials_from_env()
    return InstaclustrClient(username, api_key)


def _urllib_transport(
    method: str, url: str, headers: dict, body: bytes | None
) -> tuple[int, str]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()
    except TimeoutError as exc:
        raise InstaclustrTransportError(
            f"Instaclustr request timed out after {HTTP_TIMEOUT_S:g} seconds."
        ) from exc
    except urllib.error.URLError as exc:
        raise InstaclustrTransportError(
            f"Instaclustr request failed: {exc.reason}"
        ) from exc


class InstaclustrClient:
    def __init__(
        self,
        username: str,
        api_key: str,
        transport: Callable[[str, str, dict, bytes | None], tuple[int, str]] | None = None,
        base_url: str = BASE_URL,
        *,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._username = username
        self._api_key = api_key
        self._transport = transport or _urllib_transport
        self._base_url = base_url.rstrip("/")
        self._sleep = sleep or time.sleep

    def request(
        self,
        method: str,
        path: str,
        json_body: dict | None = None,
        *,
        retry_on_429: bool = False,
    ) -> tuple[int, object]:
        url = self._base_url + path
        credentials = base64.b64encode(
            f"{self._username}:{self._api_key}".encode()
        ).decode()
        headers = {"Authorization": f"Basic {credentials}"}
        body: bytes | None = None
        if json_body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(json_body).encode()

        status, text = self._transport(method, url, headers, body)

        if status == 429 and retry_on_429:
            self._sleep(1.0)
            status, text = self._transport(method, url, headers, body)

        if status in (401, 403) or status >= 400:
            raise InstaclustrError(status, text)

        if not text:
            return status, None
        return status, json.loads(text)

    def list_versions(self) -> object:
        _, data = self.request(
            "GET",
            "/data-sources/applications/opensearch/versions/v2/",
            retry_on_429=True,
        )
        return data

    def compatible_sizes(self) -> object:
        _, data = self.request(
            "GET",
            "/data-sources/applications/opensearch/compatible-node-sizes/v2/",
            retry_on_429=True,
        )
        return data

    def create_cluster(self, payload: dict) -> dict:
        status, data = self.request(
            "POST", CREATE_PATH, payload, retry_on_429=False
        )
        if status not in (200, 202):
            raise InstaclustrError(
                status,
                f"Unexpected create response status {status}; expected 200 or 202.",
            )
        return data

    def get_cluster(self, cluster_id: str) -> dict:
        _, data = self.request(
            "GET",
            f"{CREATE_PATH}{cluster_id}",
            retry_on_429=True,
        )
        return data


def cluster_is_running(body: dict) -> bool:
    return body.get("status") == "RUNNING" and body.get(
        "currentClusterOperationStatus"
    ) in (None, "NO_OPERATION")


def wait_until_running(
    client: InstaclustrClient,
    cluster_id: str,
    *,
    timeout_s: float = 3600.0,
    interval_s: float = 1.1,
    monotonic: Callable[[], float] | None = None,
) -> dict:
    monotonic_fn = monotonic or time.monotonic
    start = monotonic_fn()
    last_body: dict | None = None
    while True:
        last_body = client.get_cluster(cluster_id)
        if cluster_is_running(last_body):
            return last_body
        status = last_body.get("status")
        operation_status = last_body.get("currentClusterOperationStatus")
        if (
            operation_status == "OPERATION_FAILED"
            or str(status).upper() in TERMINAL_CLUSTER_STATUSES
        ):
            raise ClusterOperationError(cluster_id, status, operation_status)
        if monotonic_fn() - start >= timeout_s:
            detail = status
            if operation_status is not None:
                detail = f"{status}, currentClusterOperationStatus={operation_status}"
            raise TimeoutError(cluster_id, detail)
        client._sleep(interval_s)


def build_create_payload(
    *,
    name: str,
    cloud_provider: str,
    region: str,
    data_centre_name: str,
    network: str,
    number_of_racks: int,
    data_node_size: str,
    data_node_count: int,
    cluster_manager_node_size: str,
    dedicated_manager: bool,
    opensearch_version: str,
    sla_tier: str = "NON_PRODUCTION",
    private_network_cluster: bool = False,
    pci_compliance_mode: bool = False,
    provider_account_name: str | None = None,
    aws_settings: dict | list[dict] | None = None,
    gcp_settings: dict | list[dict] | None = None,
    azure_settings: dict | list[dict] | None = None,
) -> dict:
    if cloud_provider not in ALLOWED_CLOUD_PROVIDERS:
        raise UserInputError(f"Unsupported cloud provider: {cloud_provider}")

    supplied_settings = [
        ("awsSettings", aws_settings),
        ("gcpSettings", gcp_settings),
        ("azureSettings", azure_settings),
    ]
    if sum(value is not None for _, value in supplied_settings) > 1:
        raise UserInputError(
            "Only one provider settings block may be supplied per data centre."
        )

    data_centre: dict = {
        "cloudProvider": cloud_provider,
        "name": data_centre_name,
        "network": network,
        "numberOfRacks": number_of_racks,
        "region": region,
    }
    if provider_account_name is not None:
        data_centre["providerAccountName"] = provider_account_name
    for settings_name, settings_value in supplied_settings:
        if settings_value is not None:
            data_centre[settings_name] = _normalize_provider_settings(
                settings_name, settings_value, cloud_provider
            )

    return {
        "name": name,
        "slaTier": sla_tier,
        "privateNetworkCluster": private_network_cluster,
        "pciComplianceMode": pci_compliance_mode,
        "opensearchVersion": opensearch_version,
        "clusterManagerNodes": [
            {
                "dedicatedManager": dedicated_manager,
                "nodeSize": cluster_manager_node_size,
            }
        ],
        "dataNodes": [{"nodeCount": data_node_count, "nodeSize": data_node_size}],
        "dataCentres": [data_centre],
    }


def _normalize_provider_settings(
    settings_name: str,
    value: dict | list[dict],
    cloud_provider: str,
) -> list[dict]:
    schema = _PROVIDER_SETTINGS[settings_name]
    if cloud_provider not in schema["providers"]:
        providers = ", ".join(sorted(schema["providers"]))
        raise UserInputError(
            f"{settings_name} is not valid for cloud provider "
            f"{cloud_provider}; expected {providers}."
        )

    if isinstance(value, dict):
        normalized = [value]
    elif (
        isinstance(value, list)
        and len(value) == 1
        and isinstance(value[0], dict)
    ):
        normalized = value
    else:
        raise UserInputError(
            f"{settings_name} must be an object or a one-element array of objects."
        )

    unsupported = set(normalized[0]) - schema["keys"]
    if unsupported:
        keys = ", ".join(sorted(unsupported))
        raise UserInputError(f"Unsupported {settings_name} key(s): {keys}.")
    return normalized


def write_env(
    path: Path,
    *,
    host: str,
    port: str,
    user: str,
    password: str | None,
) -> None:
    """Atomically write connection vars with mode 0600."""
    lines = [
        f"OPENSEARCH_HOST={host}",
        f"OPENSEARCH_PORT={port}",
        f"OPENSEARCH_USER={user}",
    ]
    if password:
        lines.append(f"OPENSEARCH_PASSWORD={password}")
    content = "\n".join(lines) + "\n"

    fd, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", text=True
    )
    temporary_path = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


# Payload keys that need an explicit HCL name rather than a mechanical
# camelCase -> snake_case conversion (the Terraform provider uses the singular
# repeated block ``data_centre`` for the API's ``dataCentres`` list).
_KEY_RENAMES = {"dataCentres": "data_centre"}


def _camel_to_snake(name: str) -> str:
    if name in _KEY_RENAMES:
        return _KEY_RENAMES[name]
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _hcl_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))


def _hcl_body(data: dict, indent: int) -> str:
    pad = "  " * indent
    lines: list[str] = []
    for key, value in data.items():
        hcl_key = _camel_to_snake(key)
        if isinstance(value, dict):
            lines.append(f"{pad}{hcl_key} {{")
            lines.append(_hcl_body(value, indent + 1))
            lines.append(f"{pad}}}")
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    lines.append(f"{pad}{hcl_key} {{")
                    lines.append(_hcl_body(item, indent + 1))
                    lines.append(f"{pad}}}")
                else:
                    lines.append(f"{pad}{hcl_key} = {_hcl_scalar(item)}")
        else:
            lines.append(f"{pad}{hcl_key} = {_hcl_scalar(value)}")
    return "\n".join(lines)


def _assert_no_secrets(payload: dict) -> None:
    """Refuse to serialize a payload that carries API key material."""
    api_key = os.environ.get("INSTACLUSTR_API_KEY")

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key == "apiKey":
                    raise UnsafePayloadError(
                        "Refusing to emit Terraform: payload contains an 'apiKey' key."
                    )
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)
        elif api_key and isinstance(obj, str) and obj == api_key:
            raise UnsafePayloadError(
                "Refusing to emit Terraform: payload contains the API key value."
            )

    walk(payload)


def emit_terraform(
    out_dir: Path,
    *,
    payload: dict,
    cluster_id: str,
    template_dir: Path | None = None,
) -> Path:
    """Write ``main.tf`` and a ``README.md`` into ``out_dir``; never emit keys."""
    _assert_no_secrets(payload)
    template_dir = template_dir or TEMPLATE_DIR
    template = (template_dir / "opensearch.tf.tmpl").read_text()
    resource_body = _hcl_body(payload, indent=1)
    main_tf = template.replace("{{CLUSTER_ID}}", cluster_id).replace(
        "{{RESOURCE_BODY}}", resource_body
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "main.tf").write_text(main_tf)
    shutil.copyfile(template_dir / "README.md", out_dir / "README.md")
    return out_dir


def _endpoint(address: object, port: int) -> str | None:
    if not isinstance(address, str) or not address.strip():
        return None
    raw = address.strip()
    parsed = urllib.parse.urlsplit(
        raw if "://" in raw else f"https://{raw}"
    )
    host = parsed.hostname
    if not host:
        return None
    rendered_host = f"[{host}]" if ":" in host else host
    netloc = f"{rendered_host}:{parsed.port or port}"
    return urllib.parse.urlunsplit(
        (parsed.scheme or "https", netloc, parsed.path, parsed.query, parsed.fragment)
    )


def _node_role_kind(node: dict) -> str | None:
    roles = node.get("nodeRoles") or []
    if isinstance(roles, str):
        roles = [roles]
    normalized = {
        re.sub(r"[^A-Z0-9]+", "_", str(role).upper()).strip("_")
        for role in roles
    }
    if "OPENSEARCH_DASHBOARDS" in normalized:
        return "dashboards"
    if any(
        role == "OPENSEARCH" or role.startswith("OPENSEARCH_")
        for role in normalized
    ):
        return "opensearch"
    return None


def _append_endpoint(target: list[str], value: str | None) -> None:
    if value and value not in target:
        target.append(value)


def summarize_connection(body: dict) -> dict:
    """Derive role-based endpoints; omit credentials the API did not return."""
    opensearch_public: list[str] = []
    opensearch_private: list[str] = []
    dashboards_public: list[str] = []
    dashboards_private: list[str] = []

    for data_centre in body.get("dataCentres") or []:
        if not isinstance(data_centre, dict):
            continue
        for node in data_centre.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            kind = _node_role_kind(node)
            if kind == "opensearch":
                _append_endpoint(
                    opensearch_public, _endpoint(node.get("publicAddress"), 9200)
                )
                _append_endpoint(
                    opensearch_private, _endpoint(node.get("privateAddress"), 9200)
                )
            elif kind == "dashboards":
                _append_endpoint(
                    dashboards_public, _endpoint(node.get("publicAddress"), 5601)
                )
                _append_endpoint(
                    dashboards_private, _endpoint(node.get("privateAddress"), 5601)
                )

    public_fallback = _endpoint(body.get("publicEndpoint"), 9200)
    private_fallback = _endpoint(body.get("privateEndpoint"), 9200)
    summary = {
        "cluster_id": body.get("id"),
        "name": body.get("name"),
        "status": body.get("status"),
        "public_endpoint": public_fallback
        or (opensearch_public[0] if opensearch_public else None),
        "private_endpoint": private_fallback
        or (opensearch_private[0] if opensearch_private else None),
        "opensearch_public_endpoints": opensearch_public,
        "opensearch_private_endpoints": opensearch_private,
        "dashboards_public_endpoints": dashboards_public,
        "dashboards_private_endpoints": dashboards_private,
        "default_username": body.get("defaultUsername"),
    }
    password = body.get("defaultUserPassword")
    if password:
        summary["default_user_password"] = password
    return summary


def _print_json(obj: object) -> None:
    print(json.dumps(obj, indent=2))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="instaclustr_ops",
        description="Manage OpenSearch clusters on Instaclustr.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "compatible-sizes", help="List compatible OpenSearch node sizes."
    )
    sub.add_parser(
        "compatible-versions", help="List available OpenSearch versions."
    )

    create = sub.add_parser("create", help="Create an OpenSearch cluster.")
    create.add_argument("--name", required=True)
    create.add_argument("--cloud-provider", required=True)
    create.add_argument("--region", required=True)
    create.add_argument("--data-centre-name", required=True)
    create.add_argument("--network", required=True)
    create.add_argument("--number-of-racks", type=int, default=3)
    create.add_argument("--data-node-size", required=True)
    create.add_argument("--data-node-count", type=int, default=3)
    create.add_argument("--cluster-manager-node-size", required=True)
    create.add_argument(
        "--dedicated-manager",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    create.add_argument("--opensearch-version", required=True)
    create.add_argument("--sla-tier", default="NON_PRODUCTION")
    create.add_argument(
        "--private-network-cluster", action="store_true", default=False
    )
    create.add_argument(
        "--pci-compliance-mode", action="store_true", default=False
    )
    create.add_argument("--provider-account-name", default=None)
    create.add_argument("--aws-settings-json", default=None)
    create.add_argument("--gcp-settings-json", default=None)
    create.add_argument("--azure-settings-json", default=None)
    create.add_argument("--yes", action="store_true", default=False)

    get = sub.add_parser("get", help="Fetch a cluster by id.")
    get.add_argument("--cluster-id", required=True)

    wait = sub.add_parser("wait", help="Wait until a cluster is RUNNING.")
    wait.add_argument("--cluster-id", required=True)
    wait.add_argument("--timeout-s", type=float, default=3600.0)
    wait.add_argument("--interval-s", type=float, default=1.1)

    write = sub.add_parser("write-env", help="Write OpenSearch connection env file.")
    write.add_argument("--path", default=".env")
    write.add_argument("--host", required=True)
    write.add_argument("--port", required=True)
    write.add_argument("--user", required=True)
    write.add_argument("--password", default=None)

    tf = sub.add_parser("emit-terraform", help="Emit Terraform for a cluster.")
    tf.add_argument("--out-dir", default="instaclustr-opensearch")
    tf.add_argument("--cluster-id", required=True)
    tf.add_argument("--payload-json", required=True)

    return parser


def _parse_user_json(raw: str, option: str) -> object:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UserInputError(f"Invalid JSON for {option}: {exc}") from exc


def _parse_settings(
    raw: str | None, option: str
) -> dict | list[dict] | None:
    if raw is None:
        return None
    value = _parse_user_json(raw, option)
    if not isinstance(value, (dict, list)):
        raise UserInputError(
            f"{option} must contain an object or one-element array of objects."
        )
    return value


def _cmd_create(args: argparse.Namespace) -> int:
    client = client_from_env()
    payload = build_create_payload(
        name=args.name,
        cloud_provider=args.cloud_provider,
        region=args.region,
        data_centre_name=args.data_centre_name,
        network=args.network,
        number_of_racks=args.number_of_racks,
        data_node_size=args.data_node_size,
        data_node_count=args.data_node_count,
        cluster_manager_node_size=args.cluster_manager_node_size,
        dedicated_manager=args.dedicated_manager,
        opensearch_version=args.opensearch_version,
        sla_tier=args.sla_tier,
        private_network_cluster=args.private_network_cluster,
        pci_compliance_mode=args.pci_compliance_mode,
        provider_account_name=args.provider_account_name,
        aws_settings=_parse_settings(
            args.aws_settings_json, "--aws-settings-json"
        ),
        gcp_settings=_parse_settings(
            args.gcp_settings_json, "--gcp-settings-json"
        ),
        azure_settings=_parse_settings(
            args.azure_settings_json, "--azure-settings-json"
        ),
    )

    if not args.yes:
        plan = [
            "About to create OpenSearch cluster:",
            f"  name: {args.name}",
            f"  cloud provider: {args.cloud_provider}",
            f"  region / data centre: {args.region} / {args.data_centre_name}",
            f"  data node size: {args.data_node_size}",
            f"  cluster manager node size: {args.cluster_manager_node_size}",
            f"  opensearch version: {args.opensearch_version}",
            "Type 'yes' to proceed:",
        ]
        print("\n".join(plan), file=sys.stderr)
        if sys.stdin.readline().strip().lower() != "yes":
            return 1

    _print_json(client.create_cluster(payload))
    return 0


def _cmd_wait(args: argparse.Namespace) -> int:
    client = client_from_env()
    try:
        body = wait_until_running(
            client,
            args.cluster_id,
            timeout_s=args.timeout_s,
            interval_s=args.interval_s,
        )
    except TimeoutError as exc:
        cluster_id = exc.args[0] if exc.args else args.cluster_id
        detail = exc.args[1] if len(exc.args) > 1 else None
        # ``detail`` may carry ``currentClusterOperationStatus`` context; the
        # ``last_status`` field must be just the plain status token.
        last_status = detail.split(",", 1)[0].strip() if detail else None
        _print_json(
            {
                "error": f"Timed out waiting for cluster to reach RUNNING: {detail}",
                "cluster_id": cluster_id,
                "last_status": last_status,
            }
        )
        return 1
    except ClusterOperationError as exc:
        _print_json(
            {
                "error": str(exc),
                "cluster_id": exc.cluster_id,
                "last_status": exc.status,
                "current_cluster_operation_status": exc.operation_status,
            }
        )
        return 1
    _print_json(body)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "compatible-sizes":
            _print_json(client_from_env().compatible_sizes())
            return 0
        if args.command == "compatible-versions":
            _print_json(client_from_env().list_versions())
            return 0
        if args.command == "create":
            return _cmd_create(args)
        if args.command == "get":
            _print_json(client_from_env().get_cluster(args.cluster_id))
            return 0
        if args.command == "wait":
            return _cmd_wait(args)
        if args.command == "write-env":
            path = Path(args.path)
            write_env(
                path,
                host=args.host,
                port=args.port,
                user=args.user,
                password=args.password,
            )
            _print_json({"path": str(path)})
            return 0
        if args.command == "emit-terraform":
            payload = _parse_user_json(args.payload_json, "--payload-json")
            if not isinstance(payload, dict):
                raise UserInputError("--payload-json must contain a JSON object.")
            out_dir = emit_terraform(
                Path(args.out_dir),
                payload=payload,
                cluster_id=args.cluster_id,
            )
            _print_json({"out_dir": str(out_dir)})
            return 0
    except CredentialError as exc:
        _print_json({"error": str(exc)})
        return 2
    except InstaclustrError as exc:
        _print_json({"status": exc.status, "body": exc.body})
        return 1
    except InstaclustrTransportError as exc:
        _print_json({"error": str(exc)})
        return 1
    except UserInputError as exc:
        _print_json({"error": str(exc)})
        return 1

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
