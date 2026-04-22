# ${{ values.name }}

Auto-generated from the `deploy-service-to-kind` scaffolder template.
Edit this file in `gitops/workloads/${{ values.name }}/docs/index.md`, TechDocs
will rebuild on the next catalog refresh.

## Overview

- **Image**: `${{ values.image }}`
- **Replicas**: `${{ values.replicas }}`
- **Container port**: `${{ values.port }}`
- **Owner**: `group:default/${{ values.ownerGroup }}`

Describe what this service does, who depends on it, and what it depends on
in turn. Keep it short, the runbook below is where the operational detail
lives.

## Runbook

The Incident Analyzer will read this section first. Be explicit.

### Healthy signals

- Pod is `Running` and `Ready` (visible in the Kubernetes tab of this entity).
- CPU and memory stay below the request/limit envelope (Grafana link below).
- No restarts in the last 15 minutes.

### Common failure modes

| Symptom | Likely cause | First check |
| --- | --- | --- |
| `CrashLoopBackOff` | Bad image tag or missing config | `kubectl logs -n preview-scaffold-${{ values.name }} -l app=${{ values.name }} --previous` |
| `ImagePullBackOff` | Image tag doesn't exist on the registry | Confirm the tag on Docker Hub / GHCR. For locally-built images, `kind load docker-image <image> --name forgepath` on the dev host. |
| 5xx spike | Upstream dependency down | Check dependencies listed in Overview |

### Mitigations

- **Roll back**: revert the latest commit touching
  `gitops/workloads/${{ values.name }}/`; ArgoCD reconciles within ~60s.
- **Scale to zero**: edit `deployment.yaml` `spec.replicas: 0` and commit.
- **Force resync**: in the ArgoCD UI, click the matching Application and
  hit *Sync*.

## Dashboards

All three links are pre-filtered to the `preview-scaffold-${{ values.name }}`
namespace via the URL variable. No regex, no fall-through to "All".

- [Grafana, pod metrics](http://localhost:3000/d/cluster-pods?var-namespace=preview-scaffold-${{ values.name }})
- [Grafana, service logs](http://localhost:3000/d/service-logs?var-namespace=preview-scaffold-${{ values.name }})
- [Grafana, error explorer](http://localhost:3000/d/error-explorer?var-namespace=preview-scaffold-${{ values.name }})
- [ArgoCD application](http://localhost:8080/applications/preview-scaffold-${{ values.name }})

## Contacts

- **Owning team**: `${{ values.ownerGroup }}`
- **Escalation path**: TBD, wire up once the PagerDuty plugin is enabled
  (the `pagerduty.com/integration-key` annotation is already there as a
  placeholder).
