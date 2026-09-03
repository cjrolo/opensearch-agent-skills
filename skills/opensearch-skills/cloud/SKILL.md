---
name: cloud
description: >
  Deploy OpenSearch search applications to cloud infrastructure. Use this
  skill when the user wants to provision an OpenSearch domain or serverless
  collection on AWS, deploy search configurations, set up Bedrock connectors,
  configure IAM roles for OpenSearch, or migrate a local setup to Amazon
  OpenSearch Service or Serverless. Also use to provision a NetApp Instaclustr
  managed OpenSearch cluster via the Cluster Management API or Terraform.
  Activate even if the user says AOS, AOSS, OpenSearch Service, serverless
  collection, Bedrock connector, SigV4, AWS deployment, Instaclustr, NetApp
  Instaclustr, BYOC OpenSearch, or managed OpenSearch on Instaclustr.
compatibility: >
  AWS deployment requires AWS credentials (IAM role or access keys).
  Instaclustr provisioning requires an Instaclustr Provisioning API key.
metadata:
  author: opensearch-project
  version: "2.0"
---

# Cloud

Category skill for deploying OpenSearch to cloud infrastructure.

## Skills

| Skill | Description |
|---|---|
| [aws-setup](aws-setup/SKILL.md) | Provision and configure Amazon OpenSearch Service domains and Serverless collections, then deploy search configurations |
| [instaclustr-setup](instaclustr-setup/SKILL.md) | Provision NetApp Instaclustr managed OpenSearch (Cluster Management API, then Terraform) |

## When to Use

Read [aws-setup/SKILL.md](aws-setup/SKILL.md) when the user wants to:
- Provision an Amazon OpenSearch Service domain
- Create an Amazon OpenSearch Serverless collection
- Deploy a local search setup to AWS
- Set up Bedrock connectors for ML models
- Configure IAM roles and access policies for OpenSearch

Read [instaclustr-setup/SKILL.md](instaclustr-setup/SKILL.md) when the user wants to:
- Provision a NetApp Instaclustr managed OpenSearch cluster
- Use the Instaclustr Cluster Management API or emit Instaclustr Terraform
- Set up BYOC OpenSearch on Instaclustr (AWS_VPC, GCP, or Azure)
