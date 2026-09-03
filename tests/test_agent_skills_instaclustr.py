import json
import os
import stat
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "opensearch-skills"
    / "cloud"
    / "instaclustr-setup"
    / "scripts"
)
sys.path.insert(0, str(_SCRIPTS_DIR))
_SKILL_DIR = _SCRIPTS_DIR.parent

import instaclustr_ops
from instaclustr_ops import (
    BASE_URL,
    CredentialError,
    InstaclustrClient,
    InstaclustrError,
    build_create_payload,
    client_from_env,
    cluster_is_running,
    credentials_from_env,
    emit_terraform,
    main,
    summarize_connection,
    wait_until_running,
    write_env,
)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if not self.responses:
            raise AssertionError("unexpected extra HTTP call")
        return self.responses.pop(0)


def test_credentials_from_env_missing(monkeypatch):
    monkeypatch.delenv("INSTACLUSTR_API_USERNAME", raising=False)
    monkeypatch.delenv("INSTACLUSTR_API_KEY", raising=False)
    with pytest.raises(CredentialError):
        credentials_from_env()


def test_credentials_from_env_ok(monkeypatch):
    monkeypatch.setenv("INSTACLUSTR_API_USERNAME", "alice")
    monkeypatch.setenv("INSTACLUSTR_API_KEY", "secret-key")
    assert credentials_from_env() == ("alice", "secret-key")


def test_request_get_json_ok():
    transport = FakeTransport([(200, json.dumps({"ok": True}))])
    client = InstaclustrClient("alice", "secret-key", transport=transport)
    status, data = client.request("GET", "/data-sources/applications/opensearch/versions/v2/")
    assert status == 200
    assert data == {"ok": True}
    method, url, headers, body = transport.calls[0]
    assert method == "GET"
    assert url == BASE_URL + "/data-sources/applications/opensearch/versions/v2/"
    assert "Authorization" in headers
    assert "secret-key" not in url
    assert body is None


def test_urllib_transport_uses_finite_timeout(monkeypatch):
    seen = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(request, *, timeout):
        seen["request"] = request
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr(instaclustr_ops.urllib.request, "urlopen", fake_urlopen)
    status, text = instaclustr_ops._urllib_transport(
        "GET", "https://example.invalid", {}, None
    )
    assert status == 200
    assert text == '{"ok": true}'
    assert 0 < seen["timeout"] < 300


def test_urllib_transport_wraps_socket_timeout(monkeypatch):
    def time_out(_request, *, timeout):
        raise TimeoutError(f"socket timed out after {timeout}s")

    monkeypatch.setattr(instaclustr_ops.urllib.request, "urlopen", time_out)
    with pytest.raises(instaclustr_ops.InstaclustrTransportError, match="timed out"):
        instaclustr_ops._urllib_transport(
            "GET", "https://example.invalid", {}, None
        )


def test_request_401_raises():
    transport = FakeTransport([(401, "unauthorized")])
    client = InstaclustrClient("alice", "secret-key", transport=transport)
    with pytest.raises(InstaclustrError) as exc:
        client.request("GET", "/x")
    assert exc.value.status == 401
    assert "unauthorized" in exc.value.body


def test_request_403_raises():
    transport = FakeTransport([(403, "forbidden")])
    client = InstaclustrClient("alice", "secret-key", transport=transport)
    with pytest.raises(InstaclustrError) as exc:
        client.request("GET", "/x")
    assert exc.value.status == 403


def test_429_retries_only_when_enabled():
    transport = FakeTransport([(429, "slow"), (200, json.dumps({"a": 1}))])
    sleeps = []
    client = InstaclustrClient(
        "alice", "secret-key", transport=transport, sleep=sleeps.append
    )
    status, data = client.request("GET", "/x", retry_on_429=True)
    assert status == 200
    assert data == {"a": 1}
    assert sleeps == [1.0]


def test_429_does_not_retry_by_default():
    transport = FakeTransport([(429, "slow")])
    client = InstaclustrClient("alice", "secret-key", transport=transport)
    with pytest.raises(InstaclustrError) as exc:
        client.request("GET", "/x")
    assert exc.value.status == 429
    assert len(transport.calls) == 1


def test_build_create_payload_hosted_aws():
    payload = build_create_payload(
        name="demo-os",
        cloud_provider="AWS_VPC",
        region="US_EAST_1",
        data_centre_name="AWS_VPC_US_EAST_1",
        network="10.0.0.0/16",
        number_of_racks=3,
        data_node_size="SRH-DEV-t4g.small-5",
        data_node_count=3,
        cluster_manager_node_size="SRH-DM-DEV-t4g.small-5",
        dedicated_manager=True,
        opensearch_version="2.19.5",
    )
    assert payload["name"] == "demo-os"
    assert payload["slaTier"] == "NON_PRODUCTION"
    assert payload["pciComplianceMode"] is False
    assert payload["privateNetworkCluster"] is False
    assert payload["dataCentres"][0]["cloudProvider"] == "AWS_VPC"
    assert "providerAccountName" not in payload["dataCentres"][0]
    assert payload["clusterManagerNodes"][0]["nodeSize"] == "SRH-DM-DEV-t4g.small-5"
    assert payload["dataNodes"][0]["nodeCount"] == 3


def test_build_create_payload_byoc_aws_account():
    payload = build_create_payload(
        name="byoc-os",
        cloud_provider="AWS_VPC",
        region="US_WEST_2",
        data_centre_name="AWS_VPC_US_WEST_2",
        network="10.1.0.0/16",
        number_of_racks=3,
        data_node_size="SRH-DEV-t4g.small-5",
        data_node_count=3,
        cluster_manager_node_size="SRH-DM-DEV-t4g.small-5",
        dedicated_manager=True,
        opensearch_version="3.6.0",
        provider_account_name="my-aws-account",
        aws_settings={
            "ebsEncryptionKey": "kms-key-id",
            "customVirtualNetworkId": "vpc-123",
            "storageNetwork": "10.2.0.0/24",
        },
    )
    dc = payload["dataCentres"][0]
    assert dc["providerAccountName"] == "my-aws-account"
    assert dc["awsSettings"] == [
        {
            "ebsEncryptionKey": "kms-key-id",
            "customVirtualNetworkId": "vpc-123",
            "storageNetwork": "10.2.0.0/24",
        }
    ]


def test_build_create_payload_byoc_gcp_settings_are_array():
    payload = build_create_payload(
        name="gcp-os",
        cloud_provider="GCP",
        region="US_CENTRAL1",
        data_centre_name="GCP_US_CENTRAL1",
        network="10.3.0.0/16",
        number_of_racks=3,
        data_node_size="SRH-DEV-n2-standard-2-50",
        data_node_count=3,
        cluster_manager_node_size="SRH-DM-DEV-n2-standard-2-50",
        dedicated_manager=True,
        opensearch_version="3.6.0",
        provider_account_name="my-gcp-account",
        gcp_settings={"customVirtualNetworkId": "projects/p/global/networks/vpc"},
    )
    assert payload["dataCentres"][0]["gcpSettings"] == [
        {"customVirtualNetworkId": "projects/p/global/networks/vpc"}
    ]


def test_build_create_payload_byoc_azure_settings_are_array():
    payload = build_create_payload(
        name="azure-os",
        cloud_provider="AZURE_AZ",
        region="CENTRAL_US",
        data_centre_name="AZURE_AZ_CENTRAL_US",
        network="10.4.0.0/16",
        number_of_racks=3,
        data_node_size="SRH-DEV-Standard_D2s_v5-50",
        data_node_count=3,
        cluster_manager_node_size="SRH-DM-DEV-Standard_D2s_v5-50",
        dedicated_manager=True,
        opensearch_version="3.6.0",
        provider_account_name="my-azure-account",
        azure_settings={
            "resourceGroup": "opensearch-rg",
            "customVirtualNetworkId": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Network/virtualNetworks/vnet",
            "storageNetwork": "10.5.0.0/24",
        },
    )
    assert payload["dataCentres"][0]["azureSettings"] == [
        {
            "resourceGroup": "opensearch-rg",
            "customVirtualNetworkId": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Network/virtualNetworks/vnet",
            "storageNetwork": "10.5.0.0/24",
        }
    ]


@pytest.mark.parametrize(
    ("cloud_provider", "settings_name"),
    [
        ("GCP", "aws_settings"),
        ("AWS_VPC", "gcp_settings"),
        ("AWS_VPC", "azure_settings"),
    ],
)
def test_build_create_payload_rejects_mismatched_provider_settings(
    cloud_provider, settings_name
):
    kwargs = {
        "name": "bad-settings",
        "cloud_provider": cloud_provider,
        "region": "REGION",
        "data_centre_name": "dc",
        "network": "10.0.0.0/16",
        "number_of_racks": 3,
        "data_node_size": "data",
        "data_node_count": 3,
        "cluster_manager_node_size": "manager",
        "dedicated_manager": True,
        "opensearch_version": "3.6.0",
        settings_name: {"customVirtualNetworkId": "network-id"},
    }
    with pytest.raises(ValueError, match="cloud provider"):
        build_create_payload(**kwargs)


def test_build_create_payload_rejects_invalid_illustrative_setting_key():
    with pytest.raises(ValueError, match="Unsupported awsSettings key"):
        build_create_payload(
            name="bad-key",
            cloud_provider="AWS_VPC",
            region="US_EAST_1",
            data_centre_name="dc",
            network="10.0.0.0/16",
            number_of_racks=3,
            data_node_size="data",
            data_node_count=3,
            cluster_manager_node_size="manager",
            dedicated_manager=True,
            opensearch_version="3.6.0",
            aws_settings={"vpcId": "vpc-123"},
        )


def test_build_create_payload_rejects_multiple_provider_settings():
    with pytest.raises(ValueError, match="Only one"):
        build_create_payload(
            name="bad-settings",
            cloud_provider="AWS_VPC",
            region="US_EAST_1",
            data_centre_name="dc",
            network="10.0.0.0/16",
            number_of_racks=3,
            data_node_size="data",
            data_node_count=3,
            cluster_manager_node_size="manager",
            dedicated_manager=True,
            opensearch_version="3.6.0",
            aws_settings={"customVirtualNetworkId": "vpc-123"},
            gcp_settings={"customVirtualNetworkId": "vpc"},
        )


def test_build_create_payload_rejects_unknown_provider():
    with pytest.raises(ValueError):
        build_create_payload(
            name="x",
            cloud_provider="AWS",
            region="US_EAST_1",
            data_centre_name="x",
            network="10.0.0.0/16",
            number_of_racks=3,
            data_node_size="s",
            data_node_count=3,
            cluster_manager_node_size="s",
            dedicated_manager=True,
            opensearch_version="2.19.5",
        )


def test_list_versions_and_sizes_use_retry():
    versions = [{"applicationVersions": [{"application": "OPENSEARCH", "versions": ["2.19.5"]}]}]
    sizes = [{"cloudProviderNodeSizes": [{"cloudProvider": "AWS_VPC", "nodeSizes": ["SRH-DEV-t4g.small-5"]}]}]
    transport = FakeTransport(
        [(200, json.dumps(versions)), (200, json.dumps(sizes))]
    )
    client = InstaclustrClient("alice", "secret-key", transport=transport)
    assert client.list_versions() == versions
    assert client.compatible_sizes() == sizes
    assert "/versions/v2/" in transport.calls[0][1]
    assert "/compatible-node-sizes/v2/" in transport.calls[1][1]


def test_create_cluster_posts_payload_once():
    created = {"id": "cid-1", "status": "PROVISIONING", "name": "demo-os"}
    transport = FakeTransport([(202, json.dumps(created))])
    client = InstaclustrClient("alice", "secret-key", transport=transport)
    payload = {"name": "demo-os"}
    out = client.create_cluster(payload)
    assert out["id"] == "cid-1"
    method, url, headers, body = transport.calls[0]
    assert method == "POST"
    assert url.endswith("/resources/applications/opensearch/clusters/v2/")
    assert json.loads(body.decode()) == payload


def test_create_cluster_400_not_retried():
    transport = FakeTransport([(400, "bad node size"), (202, json.dumps({"id": "nope"}))])
    client = InstaclustrClient("alice", "secret-key", transport=transport)
    with pytest.raises(InstaclustrError) as exc:
        client.create_cluster({"name": "x"})
    assert exc.value.status == 400
    assert len(transport.calls) == 1


def test_create_cluster_429_not_retried():
    transport = FakeTransport([(429, "slow")])
    client = InstaclustrClient("alice", "secret-key", transport=transport)
    with pytest.raises(InstaclustrError) as exc:
        client.create_cluster({"name": "x"})
    assert exc.value.status == 429
    assert len(transport.calls) == 1


def test_create_cluster_rejects_unexpected_success_status():
    transport = FakeTransport([(201, json.dumps({"id": "cid-1"}))])
    client = InstaclustrClient("alice", "secret-key", transport=transport)
    with pytest.raises(InstaclustrError) as exc:
        client.create_cluster({"name": "x"})
    assert exc.value.status == 201
    assert "200 or 202" in exc.value.body
    assert len(transport.calls) == 1


def test_wait_until_running_polls_then_returns():
    provisioning = {
        "id": "cid-1",
        "status": "PROVISIONING",
        "currentClusterOperationStatus": "OPERATION_IN_PROGRESS",
    }
    running = {
        "id": "cid-1",
        "status": "RUNNING",
        "currentClusterOperationStatus": "NO_OPERATION",
    }
    transport = FakeTransport(
        [(200, json.dumps(provisioning)), (200, json.dumps(running))]
    )
    sleeps = []
    client = InstaclustrClient(
        "alice", "secret-key", transport=transport, sleep=sleeps.append
    )
    times = iter([0.0, 0.0, 2.0, 2.0])
    body = wait_until_running(
        client,
        "cid-1",
        timeout_s=10.0,
        interval_s=1.1,
        monotonic=lambda: next(times),
    )
    assert body["status"] == "RUNNING"
    assert sleeps == [1.1]


def test_wait_timeout_includes_cluster_id():
    provisioning = {
        "id": "cid-1",
        "status": "PROVISIONING",
        "currentClusterOperationStatus": "OPERATION_IN_PROGRESS",
    }
    transport = FakeTransport([(200, json.dumps(provisioning))] * 5)
    client = InstaclustrClient(
        "alice", "secret-key", transport=transport, sleep=lambda _s: None
    )
    times = iter([0.0, 0.0, 100.0])
    with pytest.raises(TimeoutError) as exc:
        wait_until_running(
            client,
            "cid-1",
            timeout_s=10.0,
            interval_s=1.1,
            monotonic=lambda: next(times),
        )
    assert "cid-1" in str(exc.value)
    assert "PROVISIONING" in str(exc.value)


def test_wait_timeout_reports_operation_status_when_running():
    stuck = {
        "id": "cid-2",
        "status": "RUNNING",
        "currentClusterOperationStatus": "OPERATION_IN_PROGRESS",
    }
    transport = FakeTransport([(200, json.dumps(stuck))] * 5)
    client = InstaclustrClient(
        "alice", "secret-key", transport=transport, sleep=lambda _s: None
    )
    times = iter([0.0, 0.0, 100.0])
    with pytest.raises(TimeoutError) as exc:
        wait_until_running(
            client,
            "cid-2",
            timeout_s=10.0,
            interval_s=1.1,
            monotonic=lambda: next(times),
        )
    assert "cid-2" in str(exc.value)
    assert "RUNNING" in str(exc.value)
    assert "OPERATION_IN_PROGRESS" in str(exc.value)


@pytest.mark.parametrize(
    "body",
    [
        {
            "id": "cid-failed-operation",
            "status": "PROVISIONING",
            "currentClusterOperationStatus": "OPERATION_FAILED",
        },
        {
            "id": "cid-failed-status",
            "status": "FAILED",
            "currentClusterOperationStatus": "NO_OPERATION",
        },
    ],
)
def test_wait_fails_immediately_on_terminal_failure(body):
    transport = FakeTransport([(200, json.dumps(body))])
    sleeps = []
    client = InstaclustrClient(
        "alice", "secret-key", transport=transport, sleep=sleeps.append
    )
    with pytest.raises(instaclustr_ops.ClusterOperationError) as exc:
        wait_until_running(client, body["id"], timeout_s=3600)
    assert body["id"] in str(exc.value)
    assert body["status"] in str(exc.value)
    assert sleeps == []
    assert len(transport.calls) == 1


def test_cluster_is_running_requires_no_operation():
    assert not cluster_is_running(
        {"status": "RUNNING", "currentClusterOperationStatus": "OPERATION_IN_PROGRESS"}
    )
    assert cluster_is_running(
        {"status": "RUNNING", "currentClusterOperationStatus": "NO_OPERATION"}
    )


def test_write_env_omits_password_when_absent(tmp_path):
    path = tmp_path / ".env"
    write_env(path, host="search.example", port="9200", user="osuser", password=None)
    text = path.read_text()
    assert "OPENSEARCH_HOST=search.example" in text
    assert "OPENSEARCH_PORT=9200" in text
    assert "OPENSEARCH_USER=osuser" in text
    assert "PASSWORD" not in text


def test_write_env_includes_password_when_set(tmp_path):
    path = tmp_path / ".env"
    write_env(path, host="h", port="9200", user="u", password="p")
    assert "OPENSEARCH_PASSWORD=p" in path.read_text()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are not portable")
def test_write_env_atomically_overwrites_with_mode_0600(tmp_path):
    path = tmp_path / ".env"
    path.write_text("OLD=value\n")
    path.chmod(0o644)

    write_env(path, host="h", port="9200", user="u", password="p")

    assert "OLD=value" not in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_env_preserves_existing_file_if_atomic_replace_fails(
    monkeypatch, tmp_path
):
    path = tmp_path / ".env"
    path.write_text("OLD=value\n")

    def fail_replace(_source, _destination):
        raise OSError("replace failed")

    monkeypatch.setattr(instaclustr_ops.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        write_env(path, host="h", port="9200", user="u", password="p")

    assert path.read_text() == "OLD=value\n"
    assert list(tmp_path.iterdir()) == [path]


def test_write_env_works_when_fchmod_is_unavailable(monkeypatch, tmp_path):
    path = tmp_path / ".env"
    monkeypatch.delattr(instaclustr_ops.os, "fchmod")
    write_env(path, host="h", port="9200", user="u", password=None)
    assert "OPENSEARCH_HOST=h" in path.read_text()


def test_emit_terraform_contains_name_provider_id(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTACLUSTR_API_KEY", "super-secret-test-key")
    payload = build_create_payload(
        name="demo-os",
        cloud_provider="AWS_VPC",
        region="US_EAST_1",
        data_centre_name="AWS_VPC_US_EAST_1",
        network="10.0.0.0/16",
        number_of_racks=3,
        data_node_size="SRH-DEV-t4g.small-5",
        data_node_count=3,
        cluster_manager_node_size="SRH-DM-DEV-t4g.small-5",
        dedicated_manager=True,
        opensearch_version="2.19.5",
    )
    out = tmp_path / "tf"
    emit_terraform(out, payload=payload, cluster_id="cid-1")
    hcl = (out / "main.tf").read_text()
    assert "demo-os" in hcl
    assert "AWS_VPC" in hcl
    assert "cid-1" in hcl
    assert "instaclustr_opensearch_cluster_v2" in hcl
    assert "super-secret-test-key" not in hcl
    assert 'variable "instaclustr_username"' in hcl
    assert 'variable "instaclustr_api_key"' in hcl
    assert hcl.count("sensitive = true") == 2
    assert (
        'terraform_key = "Instaclustr-Terraform '
        '${var.instaclustr_username}:${var.instaclustr_api_key}"'
    ) in hcl
    readme = (out / "README.md").read_text()
    assert "TF_VAR_instaclustr_username" in readme
    assert "TF_VAR_instaclustr_api_key" in readme
    assert (out / "README.md").is_file()


def test_emit_terraform_renders_provider_settings_as_nested_hcl_blocks(tmp_path):
    payload = build_create_payload(
        name="aws-os",
        cloud_provider="AWS_VPC",
        region="US_EAST_1",
        data_centre_name="AWS_VPC_US_EAST_1",
        network="10.0.0.0/16",
        number_of_racks=3,
        data_node_size="data",
        data_node_count=3,
        cluster_manager_node_size="manager",
        dedicated_manager=True,
        opensearch_version="3.6.0",
        aws_settings={
            "ebsEncryptionKey": "kms-key-id",
            "customVirtualNetworkId": "vpc-123",
            "storageNetwork": "10.2.0.0/24",
        },
    )
    out = tmp_path / "tf"
    emit_terraform(out, payload=payload, cluster_id="cid-1")
    hcl = (out / "main.tf").read_text()
    assert "aws_settings {" in hcl
    assert 'ebs_encryption_key = "kms-key-id"' in hcl
    assert 'custom_virtual_network_id = "vpc-123"' in hcl
    assert 'storage_network = "10.2.0.0/24"' in hcl


@pytest.mark.parametrize(
    ("cloud_provider", "settings_name", "settings", "expected_hcl"),
    [
        (
            "GCP",
            "gcp_settings",
            {"customVirtualNetworkId": "projects/p/global/networks/vpc"},
            [
                "gcp_settings {",
                'custom_virtual_network_id = "projects/p/global/networks/vpc"',
            ],
        ),
        (
            "AZURE",
            "azure_settings",
            {
                "resourceGroup": "opensearch-rg",
                "customVirtualNetworkId": "/subscriptions/s/vnet",
                "storageNetwork": "10.5.0.0/24",
            },
            [
                "azure_settings {",
                'resource_group = "opensearch-rg"',
                'custom_virtual_network_id = "/subscriptions/s/vnet"',
                'storage_network = "10.5.0.0/24"',
            ],
        ),
    ],
)
def test_emit_terraform_renders_gcp_and_azure_settings_blocks(
    tmp_path, cloud_provider, settings_name, settings, expected_hcl
):
    kwargs = {
        "name": "cloud-os",
        "cloud_provider": cloud_provider,
        "region": "REGION",
        "data_centre_name": "dc",
        "network": "10.0.0.0/16",
        "number_of_racks": 3,
        "data_node_size": "data",
        "data_node_count": 3,
        "cluster_manager_node_size": "manager",
        "dedicated_manager": True,
        "opensearch_version": "3.6.0",
        settings_name: settings,
    }
    payload = build_create_payload(**kwargs)
    out = tmp_path / cloud_provider.lower()
    emit_terraform(out, payload=payload, cluster_id="cid-1")
    hcl = (out / "main.tf").read_text()
    for expected in expected_hcl:
        assert expected in hcl


def test_emit_terraform_rejects_nested_api_key(tmp_path):
    with pytest.raises(ValueError, match="apiKey"):
        emit_terraform(
            tmp_path / "tf",
            payload={"dataCentres": [{"awsSettings": [{"apiKey": "secret"}]}]},
            cluster_id="cid-1",
        )


def test_emit_terraform_rejects_value_equal_to_api_key(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("INSTACLUSTR_API_KEY", "actual-api-secret")
    with pytest.raises(ValueError, match="API key value"):
        emit_terraform(
            tmp_path / "tf",
            payload={"description": "actual-api-secret"},
            cluster_id="cid-1",
        )


def test_summarize_connection_derives_role_based_endpoints():
    body = {
        "id": "cid-1",
        "name": "demo-os",
        "status": "RUNNING",
        "defaultUsername": "icopensearch",
        "opensearchDashboards": [{"nodeSize": "dashboard-size"}],
        "dataCentres": [
            {
                "nodes": [
                    {
                        "nodeRoles": ["OPENSEARCH"],
                        "publicAddress": "1.2.3.4",
                        "privateAddress": "10.0.0.4",
                    },
                    {
                        "nodeRoles": ["OPENSEARCH_DATA_AND_INGEST"],
                        "publicAddress": "1.2.3.5",
                        "privateAddress": "10.0.0.5",
                    },
                    {
                        "nodeRoles": ["opensearch-dashboards"],
                        "publicAddress": "1.2.3.6",
                        "privateAddress": "10.0.0.6",
                    },
                ]
            }
        ],
    }
    summary = summarize_connection(body)
    assert summary["cluster_id"] == "cid-1"
    assert summary["public_endpoint"] == "https://1.2.3.4:9200"
    assert summary["private_endpoint"] == "https://10.0.0.4:9200"
    assert summary["opensearch_public_endpoints"] == [
        "https://1.2.3.4:9200",
        "https://1.2.3.5:9200",
    ]
    assert summary["opensearch_private_endpoints"] == [
        "https://10.0.0.4:9200",
        "https://10.0.0.5:9200",
    ]
    assert summary["dashboards_public_endpoints"] == ["https://1.2.3.6:5601"]
    assert summary["dashboards_private_endpoints"] == ["https://10.0.0.6:5601"]
    assert "dashboards" not in summary
    assert "default_user_password" not in summary


def test_summarize_connection_prefers_top_level_endpoint_fallbacks():
    summary = summarize_connection(
        {
            "id": "cid-1",
            "publicEndpoint": "https://public.example",
            "privateEndpoint": "private.example:9443",
            "dataCentres": [],
        }
    )
    assert summary["public_endpoint"] == "https://public.example:9200"
    assert summary["private_endpoint"] == "https://private.example:9443"


def test_skill_docs_describe_role_based_endpoints_and_terminal_failures():
    skill = (_SKILL_DIR / "SKILL.md").read_text()
    api = (_SKILL_DIR / "reference" / "api.md").read_text()
    assert "nodeRoles" in skill
    assert "9200" in skill
    assert "5601" in skill
    assert "OPERATION_FAILED" in api
    assert "30" in api and "socket" in api.lower()


def test_provider_docs_use_current_array_schema_without_invalid_keys():
    providers = (_SKILL_DIR / "reference" / "providers.md").read_text()
    assert '"awsSettings": [{' in providers
    assert '"gcpSettings": [{' in providers
    assert '"azureSettings": [{' in providers
    assert "ebsEncryptionKey" in providers
    assert "customVirtualNetworkId" in providers
    assert "storageNetwork" in providers
    assert "resourceGroup" in providers
    assert '"vpcId"' not in providers
    assert "customVpcId" not in providers
    assert '"encryptionKey"' not in providers


def test_terraform_docs_use_tf_var_credentials():
    terraform = (_SKILL_DIR / "reference" / "terraform.md").read_text()
    assert "TF_VAR_instaclustr_username" in terraform
    assert "TF_VAR_instaclustr_api_key" in terraform
    assert (
        'terraform_key = "Instaclustr-Terraform '
        '${var.instaclustr_username}:${var.instaclustr_api_key}"'
    ) in terraform


# --- CLI (main) -----------------------------------------------------------


def _set_creds(monkeypatch):
    monkeypatch.setenv("INSTACLUSTR_API_USERNAME", "alice")
    monkeypatch.setenv("INSTACLUSTR_API_KEY", "secret-key")


class _Stdin:
    def __init__(self, text):
        self._text = text

    def readline(self):
        return self._text

    def read(self):
        raise AssertionError("interactive confirmation must read one line only")


def test_help_exits_zero():
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_client_from_env_uses_credentials(monkeypatch):
    _set_creds(monkeypatch)
    client = client_from_env()
    assert isinstance(client, InstaclustrClient)


def test_missing_credentials_exits_two(monkeypatch, capsys):
    monkeypatch.delenv("INSTACLUSTR_API_USERNAME", raising=False)
    monkeypatch.delenv("INSTACLUSTR_API_KEY", raising=False)
    code = main(["compatible-sizes"])
    assert code == 2
    out = json.loads(capsys.readouterr().out)
    assert "error" in out


def test_compatible_sizes_prints_json(monkeypatch, capsys):
    _set_creds(monkeypatch)
    sizes = [{"cloudProviderNodeSizes": []}]
    transport = FakeTransport([(200, json.dumps(sizes))])
    monkeypatch.setattr(
        "instaclustr_ops.client_from_env",
        lambda: InstaclustrClient("alice", "secret-key", transport=transport),
    )
    code = main(["compatible-sizes"])
    assert code == 0
    assert json.loads(capsys.readouterr().out) == sizes


def test_compatible_versions_prints_json(monkeypatch, capsys):
    _set_creds(monkeypatch)
    versions = [{"applicationVersions": []}]
    transport = FakeTransport([(200, json.dumps(versions))])
    monkeypatch.setattr(
        "instaclustr_ops.client_from_env",
        lambda: InstaclustrClient("alice", "secret-key", transport=transport),
    )
    code = main(["compatible-versions"])
    assert code == 0
    assert json.loads(capsys.readouterr().out) == versions


def test_create_without_yes_does_not_post(monkeypatch, capsys):
    _set_creds(monkeypatch)
    transport = FakeTransport([(202, json.dumps({"id": "should-not-run"}))])
    monkeypatch.setattr(
        "instaclustr_ops.client_from_env",
        lambda: InstaclustrClient("alice", "secret-key", transport=transport),
    )
    monkeypatch.setattr("sys.stdin", _Stdin("no\n"))
    code = main([
        "create",
        "--name", "demo-os",
        "--cloud-provider", "AWS_VPC",
        "--region", "US_EAST_1",
        "--data-centre-name", "AWS_VPC_US_EAST_1",
        "--network", "10.0.0.0/16",
        "--data-node-size", "SRH-DEV-t4g.small-5",
        "--cluster-manager-node-size", "SRH-DM-DEV-t4g.small-5",
        "--opensearch-version", "2.19.5",
    ])
    assert code == 1
    assert transport.calls == []
    err = capsys.readouterr().err
    assert "demo-os" in err
    assert "AWS_VPC" in err
    assert "cost" not in err.lower()
    assert "money" not in err.lower()
    assert "billing" not in err.lower()


def test_create_yes_posts(monkeypatch, capsys):
    _set_creds(monkeypatch)
    transport = FakeTransport([(202, json.dumps({"id": "cid-1", "name": "demo-os"}))])
    monkeypatch.setattr(
        "instaclustr_ops.client_from_env",
        lambda: InstaclustrClient("alice", "secret-key", transport=transport),
    )
    code = main([
        "create", "--yes",
        "--name", "demo-os",
        "--cloud-provider", "AWS_VPC",
        "--region", "US_EAST_1",
        "--data-centre-name", "AWS_VPC_US_EAST_1",
        "--network", "10.0.0.0/16",
        "--data-node-size", "SRH-DEV-t4g.small-5",
        "--cluster-manager-node-size", "SRH-DM-DEV-t4g.small-5",
        "--opensearch-version", "2.19.5",
    ])
    assert code == 0
    assert len(transport.calls) == 1
    assert json.loads(capsys.readouterr().out)["id"] == "cid-1"


def test_create_stdin_yes_posts(monkeypatch, capsys):
    _set_creds(monkeypatch)
    transport = FakeTransport([(202, json.dumps({"id": "cid-9"}))])
    monkeypatch.setattr(
        "instaclustr_ops.client_from_env",
        lambda: InstaclustrClient("alice", "secret-key", transport=transport),
    )
    monkeypatch.setattr("sys.stdin", _Stdin("  YES\n"))
    code = main([
        "create",
        "--name", "demo-os",
        "--cloud-provider", "AWS_VPC",
        "--region", "US_EAST_1",
        "--data-centre-name", "AWS_VPC_US_EAST_1",
        "--network", "10.0.0.0/16",
        "--data-node-size", "SRH-DEV-t4g.small-5",
        "--cluster-manager-node-size", "SRH-DM-DEV-t4g.small-5",
        "--opensearch-version", "2.19.5",
    ])
    assert code == 0
    assert len(transport.calls) == 1


def test_create_error_exits_one_with_status_body(monkeypatch, capsys):
    _set_creds(monkeypatch)
    transport = FakeTransport([(400, "bad node size")])
    monkeypatch.setattr(
        "instaclustr_ops.client_from_env",
        lambda: InstaclustrClient("alice", "secret-key", transport=transport),
    )
    code = main([
        "create", "--yes",
        "--name", "demo-os",
        "--cloud-provider", "AWS_VPC",
        "--region", "US_EAST_1",
        "--data-centre-name", "AWS_VPC_US_EAST_1",
        "--network", "10.0.0.0/16",
        "--data-node-size", "SRH-DEV-t4g.small-5",
        "--cluster-manager-node-size", "SRH-DM-DEV-t4g.small-5",
        "--opensearch-version", "2.19.5",
    ])
    assert code == 1
    assert len(transport.calls) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == 400
    assert out["body"] == "bad node size"


def test_get_prints_json(monkeypatch, capsys):
    _set_creds(monkeypatch)
    body = {"id": "cid-1", "status": "RUNNING"}
    transport = FakeTransport([(200, json.dumps(body))])
    monkeypatch.setattr(
        "instaclustr_ops.client_from_env",
        lambda: InstaclustrClient("alice", "secret-key", transport=transport),
    )
    code = main(["get", "--cluster-id", "cid-1"])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["id"] == "cid-1"


def test_wait_running_prints_json(monkeypatch, capsys):
    _set_creds(monkeypatch)
    running = {
        "id": "cid-1",
        "status": "RUNNING",
        "currentClusterOperationStatus": "NO_OPERATION",
    }
    transport = FakeTransport([(200, json.dumps(running))])
    monkeypatch.setattr(
        "instaclustr_ops.client_from_env",
        lambda: InstaclustrClient(
            "alice", "secret-key", transport=transport, sleep=lambda _s: None
        ),
    )
    code = main(["wait", "--cluster-id", "cid-1", "--timeout-s", "10", "--interval-s", "0"])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "RUNNING"


def test_wait_timeout_exits_one(monkeypatch, capsys):
    _set_creds(monkeypatch)
    provisioning = {
        "id": "cid-1",
        "status": "PROVISIONING",
        "currentClusterOperationStatus": "OPERATION_IN_PROGRESS",
    }
    transport = FakeTransport([(200, json.dumps(provisioning))] * 5)
    monkeypatch.setattr(
        "instaclustr_ops.client_from_env",
        lambda: InstaclustrClient(
            "alice", "secret-key", transport=transport, sleep=lambda _s: None
        ),
    )
    code = main(["wait", "--cluster-id", "cid-1", "--timeout-s", "0", "--interval-s", "0"])
    assert code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["cluster_id"] == "cid-1"
    assert out["last_status"] == "PROVISIONING"
    assert "error" in out
    assert "OPERATION_IN_PROGRESS" in out["error"]


def test_wait_terminal_failure_exits_one_with_json(monkeypatch, capsys):
    _set_creds(monkeypatch)
    failed = {
        "id": "cid-1",
        "status": "PROVISIONING",
        "currentClusterOperationStatus": "OPERATION_FAILED",
    }
    transport = FakeTransport([(200, json.dumps(failed))])
    monkeypatch.setattr(
        "instaclustr_ops.client_from_env",
        lambda: InstaclustrClient(
            "alice", "secret-key", transport=transport, sleep=lambda _s: None
        ),
    )
    code = main(["wait", "--cluster-id", "cid-1"])
    assert code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["cluster_id"] == "cid-1"
    assert out["last_status"] == "PROVISIONING"
    assert out["current_cluster_operation_status"] == "OPERATION_FAILED"
    assert "terminal failure" in out["error"].lower()


def test_write_env_command(monkeypatch, tmp_path, capsys):
    _set_creds(monkeypatch)
    path = tmp_path / ".env"
    code = main([
        "write-env",
        "--path", str(path),
        "--host", "search.example",
        "--port", "9200",
        "--user", "osuser",
        "--password", "pw",
    ])
    assert code == 0
    text = path.read_text()
    assert "OPENSEARCH_HOST=search.example" in text
    assert "OPENSEARCH_PASSWORD=pw" in text


def test_emit_terraform_command(monkeypatch, tmp_path, capsys):
    _set_creds(monkeypatch)
    payload = build_create_payload(
        name="demo-os",
        cloud_provider="AWS_VPC",
        region="US_EAST_1",
        data_centre_name="AWS_VPC_US_EAST_1",
        network="10.0.0.0/16",
        number_of_racks=3,
        data_node_size="SRH-DEV-t4g.small-5",
        data_node_count=3,
        cluster_manager_node_size="SRH-DM-DEV-t4g.small-5",
        dedicated_manager=True,
        opensearch_version="2.19.5",
    )
    out_dir = tmp_path / "tf"
    code = main([
        "emit-terraform",
        "--out-dir", str(out_dir),
        "--cluster-id", "cid-1",
        "--payload-json", json.dumps(payload),
    ])
    assert code == 0
    assert (out_dir / "main.tf").is_file()


def test_compatible_sizes_error_exits_one(monkeypatch, capsys):
    _set_creds(monkeypatch)
    transport = FakeTransport([(500, "boom")])
    monkeypatch.setattr(
        "instaclustr_ops.client_from_env",
        lambda: InstaclustrClient("alice", "secret-key", transport=transport),
    )
    code = main(["compatible-sizes"])
    assert code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == 500
    assert out["body"] == "boom"


def test_transport_timeout_exits_one_with_json(monkeypatch, capsys):
    _set_creds(monkeypatch)

    def time_out(*_args):
        raise instaclustr_ops.InstaclustrTransportError(
            "Instaclustr request timed out after 30 seconds."
        )

    monkeypatch.setattr(
        "instaclustr_ops.client_from_env",
        lambda: InstaclustrClient("alice", "secret-key", transport=time_out),
    )
    code = main(["compatible-sizes"])
    assert code == 1
    out = json.loads(capsys.readouterr().out)
    assert "timed out" in out["error"]


def test_invalid_api_json_is_not_misreported_as_user_input(monkeypatch):
    _set_creds(monkeypatch)
    transport = FakeTransport([(200, "not-json")])
    monkeypatch.setattr(
        "instaclustr_ops.client_from_env",
        lambda: InstaclustrClient("alice", "secret-key", transport=transport),
    )
    with pytest.raises(json.JSONDecodeError):
        main(["compatible-sizes"])


def test_compatible_versions_error_exits_one(monkeypatch, capsys):
    _set_creds(monkeypatch)
    transport = FakeTransport([(503, "unavailable")])
    monkeypatch.setattr(
        "instaclustr_ops.client_from_env",
        lambda: InstaclustrClient("alice", "secret-key", transport=transport),
    )
    code = main(["compatible-versions"])
    assert code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == 503
    assert out["body"] == "unavailable"


def test_get_error_exits_one(monkeypatch, capsys):
    _set_creds(monkeypatch)
    transport = FakeTransport([(404, "not found")])
    monkeypatch.setattr(
        "instaclustr_ops.client_from_env",
        lambda: InstaclustrClient("alice", "secret-key", transport=transport),
    )
    code = main(["get", "--cluster-id", "cid-x"])
    assert code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == 404
    assert out["body"] == "not found"


def test_wait_polling_error_exits_one(monkeypatch, capsys):
    _set_creds(monkeypatch)
    transport = FakeTransport([(500, "server error")])
    monkeypatch.setattr(
        "instaclustr_ops.client_from_env",
        lambda: InstaclustrClient(
            "alice", "secret-key", transport=transport, sleep=lambda _s: None
        ),
    )
    code = main(["wait", "--cluster-id", "cid-1", "--timeout-s", "10", "--interval-s", "0"])
    assert code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == 500
    assert out["body"] == "server error"


def test_write_env_command_prints_json(monkeypatch, tmp_path, capsys):
    _set_creds(monkeypatch)
    path = tmp_path / ".env"
    code = main([
        "write-env",
        "--path", str(path),
        "--host", "h",
        "--port", "9200",
        "--user", "u",
        "--password", "p",
    ])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == str(path)


def test_emit_terraform_command_prints_json(monkeypatch, tmp_path, capsys):
    _set_creds(monkeypatch)
    payload = build_create_payload(
        name="demo-os",
        cloud_provider="AWS_VPC",
        region="US_EAST_1",
        data_centre_name="AWS_VPC_US_EAST_1",
        network="10.0.0.0/16",
        number_of_racks=3,
        data_node_size="SRH-DEV-t4g.small-5",
        data_node_count=3,
        cluster_manager_node_size="SRH-DM-DEV-t4g.small-5",
        dedicated_manager=True,
        opensearch_version="2.19.5",
    )
    out_dir = tmp_path / "tf"
    code = main([
        "emit-terraform",
        "--out-dir", str(out_dir),
        "--cluster-id", "cid-1",
        "--payload-json", json.dumps(payload),
    ])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["out_dir"] == str(out_dir)


def test_create_invalid_provider_exits_one(monkeypatch, capsys):
    _set_creds(monkeypatch)
    transport = FakeTransport([])
    monkeypatch.setattr(
        "instaclustr_ops.client_from_env",
        lambda: InstaclustrClient("alice", "secret-key", transport=transport),
    )
    code = main([
        "create", "--yes",
        "--name", "demo-os",
        "--cloud-provider", "AWS",
        "--region", "US_EAST_1",
        "--data-centre-name", "AWS_VPC_US_EAST_1",
        "--network", "10.0.0.0/16",
        "--data-node-size", "SRH-DEV-t4g.small-5",
        "--cluster-manager-node-size", "SRH-DM-DEV-t4g.small-5",
        "--opensearch-version", "2.19.5",
    ])
    assert code == 1
    assert transport.calls == []
    out = json.loads(capsys.readouterr().out)
    assert "error" in out


def test_create_invalid_settings_json_exits_one(monkeypatch, capsys):
    _set_creds(monkeypatch)
    transport = FakeTransport([])
    monkeypatch.setattr(
        "instaclustr_ops.client_from_env",
        lambda: InstaclustrClient("alice", "secret-key", transport=transport),
    )
    code = main([
        "create", "--yes",
        "--name", "demo-os",
        "--cloud-provider", "AWS_VPC",
        "--region", "US_EAST_1",
        "--data-centre-name", "AWS_VPC_US_EAST_1",
        "--network", "10.0.0.0/16",
        "--data-node-size", "SRH-DEV-t4g.small-5",
        "--cluster-manager-node-size", "SRH-DM-DEV-t4g.small-5",
        "--opensearch-version", "2.19.5",
        "--aws-settings-json", "{not valid json",
    ])
    assert code == 1
    assert transport.calls == []
    out = json.loads(capsys.readouterr().out)
    assert "error" in out


def test_emit_terraform_invalid_payload_json_exits_one(monkeypatch, tmp_path, capsys):
    _set_creds(monkeypatch)
    out_dir = tmp_path / "tf"
    code = main([
        "emit-terraform",
        "--out-dir", str(out_dir),
        "--cluster-id", "cid-1",
        "--payload-json", "{bad json",
    ])
    assert code == 1
    out = json.loads(capsys.readouterr().out)
    assert "error" in out


def test_emit_terraform_internal_value_error_is_not_masked(
    monkeypatch, tmp_path
):
    _set_creds(monkeypatch)
    monkeypatch.setattr(
        "instaclustr_ops.emit_terraform",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("unexpected emitter defect")
        ),
    )
    with pytest.raises(ValueError, match="unexpected emitter defect"):
        main(
            [
                "emit-terraform",
                "--out-dir",
                str(tmp_path / "tf"),
                "--cluster-id",
                "cid-1",
                "--payload-json",
                "{}",
            ]
        )
