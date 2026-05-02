# Atlas Deploy Readiness Report

Scope: Helm chart (`Optic_Count/helm/atlas`) + secrets wiring, targeting Kubernetes (Kind per values.yaml).
Reviewed: Chart.yaml, values.yaml, all templates, secret.yaml, configmap.yaml, web-deployment.yaml, postgres-statefulset.yaml, Dockerfile, docker-compose.yml, .env, .env.example, atlas_web_app.py env reads, demo_auth_ai.py, atlas_data_loader.py.

Verdict: **NOT READY**. Four deploy blockers, several config mismatches that will cause silent wrong behavior, and one live credential exposure that needs action before anything else.

---

## P0 - Blockers (deploy will fail or leak secrets)

### 1. Live API keys committed to `Optic_Count/.env`
The `.env` file in `Optic_Count/` contains what look like real production credentials:
- `ANTHROPIC_API_KEY=sk-ant-api03-QQy_TcZD...`
- `NETBOX_API_TOKEN=cJNeuYskMRbWW7J2emsrdiltsJ334SsdAnniYl5s`
- `DEMO_TOKEN_SECRET=fb8edeb7946876695a0ff96c4787938e2acc3a9a55b009862f1e6eb703d958ea`

`.gitignore` does exclude `.env`, so git is not tracking it, but the file is still sitting on disk and may have been shared via other channels. Rotate these three credentials immediately, then regenerate locally. Treat this as a leak until proven otherwise.

### 2. Health probes point to a route that does not exist in `atlas_web_app.py`
`values.yaml` sets `web.healthPath: /api/health`. Grep of `atlas_web_app.py` returns zero matches for `health` anywhere. The liveness and readiness probes will never get a 200, Kubernetes will kill the pod in a loop, and the rollout will never go ready.
`demo_web_app.py` does have `/api/health` at line ~714, but helm is packaged around atlas_web_app (per the Dockerfile and naming). Either:
- Add `@app.route("/api/health")` to `atlas_web_app.py` returning `{"ok": true}`, or
- Change the Dockerfile CMD to run the module that has the health endpoint (see item 3), or
- Change `web.healthPath` to a known working route.

### 3. Dockerfile runs `demo_web_app.py`, not `atlas_web_app.py`
Last line of `Dockerfile`:
```
CMD ["python", "demo_web_app.py"]
```
But the helm chart, env vars (DB_HOST, DB_NAME, etc.), and the whole postgres stack are wired for the atlas pipeline. Two problems in one:
- Wrong module: should be `atlas_web_app:app` (the Flask app the helm secrets/configmap were designed for).
- Bare `python` = Flask dev server, single-threaded, not production safe. `requirements.txt` already pulls gunicorn but nothing uses it.

Recommended CMD:
```
CMD ["gunicorn", "-b", "0.0.0.0:5050", "-w", "4", "--access-logfile", "-", "atlas_web_app:app"]
```

### 4. Env var name mismatch: `ATLAS_UPLOAD_DIR` vs `DEMO_UPLOAD_DIR`
`atlas_web_app.py:30` reads `ATLAS_UPLOAD_DIR`. ConfigMap sets `DEMO_UPLOAD_DIR`. Result: the app ignores the configmap value and falls back to `./uploads`.
This works by coincidence today because `./uploads` relative to `/app` = `/app/uploads`, which is where the PVC is mounted. Fragile and will break the first time someone changes `values.yaml`.
Fix: either rename the env var in code to match the configmap, or add `ATLAS_UPLOAD_DIR` to the configmap. Pick one and normalize.

---

## P1 - Will work but wrong behavior

### 5. `GOOGLE_SA_KEY_JSON` secret is not read by any code path
`secret.yaml` and `values.yaml` both expose `GOOGLE_SA_KEY_JSON` (inline JSON content). But `gsheet_fetcher.py:247` reads `GOOGLE_SA_KEY_PATH` (a file path). Secret is plumbed but never consumed.
Fix options: (a) drop the secret entry if you are not using gsheet in k8s, (b) mount the secret as a file via a volume and set `GOOGLE_SA_KEY_PATH` to that path, or (c) update the code to accept JSON content via an env var.

### 6. Missing env vars in ConfigMap that the code reads
The code reads these but they are not in ConfigMap or Secret:
- `NETBOX_API_TOKEN` (required by `Netbox_query.py:17`, uses `os.environ[...]` with no default - will raise KeyError)
- `NETBOX_BASE_URL`
- `OPENAI_MODEL` (falls back to gpt-4o-mini)
- `LLM_RETRY_ATTEMPTS`, `LLM_RETRY_BASE_WAIT`, `LLM_RETRY_MAX_WAIT`
- `LLM_TIMEOUT_SECONDS`
- `LLM_FALLBACK_ENABLED`
- `LLM_CACHE_TTL_SECONDS`
- `ANTHROPIC_MODEL` in `values.yaml` is `claude-sonnet-4-6` which is fine, just confirming it plumbs through.
Decide whether Netbox and the resilience knobs are in scope for this deploy. If yes, add them to configmap (non-secret) and add NETBOX_API_TOKEN to the secret.

### 7. `values.yaml` default passwords are empty strings
`secrets.dbPassword: ""` and `secrets.demoTokenSecret: ""` default to empty. If someone runs `helm install` without overrides:
- Postgres StatefulSet starts with no password (or may refuse to start depending on image version).
- Demo token signing uses an empty HMAC key - any token validates.
Add a preflight check or use `required` in the template:
```yaml
DB_PASSWORD: {{ required "secrets.dbPassword must be set" .Values.secrets.dbPassword | quote }}
```

### 8. `postgres-statefulset.yaml` mounts schema configmap unconditionally
Lines 72-74 mount the `-schema` configmap every time, but the configmap is only created when `schemaInit.enabled: true`. If someone sets `schemaInit.enabled: false`, the postgres pod will crash looping on a missing volume.
Fix: wrap the volume and volumeMount in `{{- if .Values.schemaInit.enabled }}` blocks, or remove the toggle since the schema is needed on first boot anyway.

### 9. `DB_PORT` consistency check
Helm configmap sets `DB_PORT: "5432"` (from `postgres.port`). Good - inside the cluster we talk to the postgres service on its real port, not the 9000 host mapping used in docker-compose. Code default is 9000 (`atlas_data_loader.py:71`), so the configmap value has to win. It does via `envFrom`. Just verify on deploy that pods log the right port.

---

## P2 - Hygiene / production hardening

### 10. `imagePullPolicy: Never` is Kind-specific
Fine for local Kind. Breaks any real cluster where the image has to be pulled. Make it environment-conditional (`values-prod.yaml` override to `IfNotPresent`).

### 11. No PodSecurityContext, no NetworkPolicy, no PDB, no HPA
- Dockerfile runs as non-root user `atlas`, good. But pod spec should also assert `securityContext.runAsNonRoot: true` and `readOnlyRootFilesystem: true` where possible.
- No NetworkPolicy means the web pod can hit anything in-cluster. For a POC that is fine; for prod, lock down egress to the postgres service and external LLM endpoints only.
- No PodDisruptionBudget. With `replicaCount: 2` and `maxUnavailable: 0` on rolling update you get graceful rollouts, but a node drain could take both pods.
- No HPA. Two replicas is static.

### 12. Postgres is a single-replica StatefulSet with no backup
Production path needs either a managed Postgres (CloudSQL / RDS / whatever CoreWeave exposes) or a real HA operator (CNPG, Zalando). Current chart is a dev-only database.

### 13. `.env.example` is out of sync with `.env`
`.env.example` (tracked) is missing: `ANTHROPIC_MODEL`, `ANTHROPIC_API_KEY`, `NETBOX_BASE_URL`, `NETBOX_API_TOKEN`, `DATABASE_URL`, `POSTGRES_PASSWORD`, and the whole `LLM_RESILIENCE` block that `.env` has. New devs setting up locally will hit silent defaults.

### 14. Ingress disabled with empty hosts list
Fine as long as access is via `kubectl port-forward`. If anyone ever wants external access, they have to fill in `ingress.hosts`, className, and annotations. Flag for handoff.

### 15. `.md` docs drift from current state
- `README.md` at repo root is 71 bytes and says nothing about Atlas.
- `recap.md` is dated 2026-03-28 and references files and flow that have shifted (e.g., `llm_resilience.py` in recap, but I saw the resilience decorators folded into `demo_auth_ai.py`).
- `Change.md` references `/Users/lwells/Desktop/...` absolute paths - dev-machine artifacts that should be cleaned up.
- `Optic_Count/README.md` still talks about OPENAI as primary and PIN+JSON token demo flow. Current stack is Anthropic-primary with Postgres-backed atlas app.
None of these block deploy, but they will confuse whoever inherits this.

---

## Minimum punch list before `helm install`

1. Rotate the three leaked creds in `.env` (Anthropic, Netbox, demo token). Do this first.
2. Add `/api/health` to `atlas_web_app.py`.
3. Fix Dockerfile CMD to `gunicorn atlas_web_app:app`.
4. Resolve the `ATLAS_UPLOAD_DIR` vs `DEMO_UPLOAD_DIR` mismatch (pick one, normalize).
5. Either wire up `NETBOX_API_TOKEN` through secret/configmap or guard the Netbox imports behind a feature flag so they do not run at import time.
6. Make `schemaInit` either always-on or gate the volume/mount properly in `postgres-statefulset.yaml`.
7. Add `required` guard on `secrets.dbPassword` and `secrets.demoTokenSecret` in `secret.yaml`.
8. Decide: drop `GOOGLE_SA_KEY_JSON` secret, or mount it as a file and set `GOOGLE_SA_KEY_PATH`.

After that, a dry run is worth doing:
```
helm template atlas ./helm/atlas --set secrets.dbPassword=x --set secrets.demoTokenSecret=y --set secrets.anthropicApiKey=z | kubectl apply --dry-run=client -f -
```
then `kubectl describe pod` the web pod to confirm envFrom resolved everything the code expects.

---

## Files I looked at
- `Optic_Count/helm/atlas/Chart.yaml`, `values.yaml`, all `templates/*`
- `Optic_Count/Dockerfile`, `docker-compose.yml`, `requirements.txt`
- `Optic_Count/.env`, `.env.example`
- `Optic_Count/atlas_web_app.py`, `demo_web_app.py`, `demo_auth_ai.py`, `atlas_data_loader.py`, `gsheet_fetcher.py`, `Netbox_query.py`
- `DCT_Scripts/CLAUDE.md`, `README.md`, `ARCHITECTURE_GUIDE.md`
- `Optic_Count/README.md`, `Change.md`, `Project_Overview.md`
- git status and last 20 commits on `lamars-branch`

---

# Deploy Guide (Kind / Helm)

Last updated: 2026-04-19

## Prerequisites

- Docker Desktop running
- `kind`, `kubectl`, `helm` installed
- Working directory: `~/Atlas/DCT_Scripts/Optic_Count`

## values-local.yaml

Create this file in `Optic_Count/` (do not commit):

```yaml
secrets:
  dbPassword: "atlas-rocks"
  demoTokenSecret: "d32e404142fc0be7d0ef85b05cfa04495ab4cc3887f798c404c1d0a6e1ce4bd0"
  anthropicApiKey: "sk-ant-..."      # your real key
  netboxApiToken: ""                 # fill in if testing Netbox streaming
```

**Important:** `demoTokenSecret` must be a real hex string. Helm does not expand shell commands. Generate with:
```
openssl rand -hex 32
```

## Full Clean Deploy (from scratch)

```bash
# 1. Tear down any existing cluster
kind delete cluster

# 2. Build the Docker image
cd ~/Atlas/DCT_Scripts/Optic_Count
docker build -t atlas-web:1.0.0 .

# 3. Create the Kind cluster
kind create cluster --name kind

# 4. Load the image into Kind
kind load docker-image atlas-web:1.0.0

# 5. Deploy with Helm
helm install atlas ./helm/atlas -f values-local.yaml

# 6. Wait for pods (Ctrl+C when all show Running)
kubectl get pods -w

# 7. Port forward
kubectl port-forward svc/atlas-atlas-web 5050:5050
```

Browse to `http://localhost:5050`. Default PIN: `123456`

## Quick Rebuild (code changes only)

```bash
cd ~/Atlas/DCT_Scripts/Optic_Count
docker build -t atlas-web:1.0.0 .
kind load docker-image atlas-web:1.0.0
kubectl rollout restart deployment atlas-atlas-web
kubectl port-forward svc/atlas-atlas-web 5050:5050
```

Hard refresh the browser (Cmd+Shift+R) after port-forward reconnects.

## Loading Cutsheets into Postgres

```bash
kubectl exec -it deploy/atlas-atlas-web -- \
  python atlas_data_loader.py --file /app/uploads/QNC01.xlsx --site QCY

kubectl exec -it deploy/atlas-atlas-web -- \
  python atlas_data_loader.py --file /app/uploads/ELD01.xlsx --site ELD
```

Verify:
```bash
kubectl exec -it atlas-atlas-postgres-0 -- \
  psql -U atlas -d atlas -c "SELECT site_code, count(*) FROM devices GROUP BY site_code;"
```

## Useful Debug Commands

```bash
kubectl get pods
kubectl logs <pod-name> --tail=50 -c web
curl http://localhost:5050/api/health
kubectl describe pod <pod-name>
kubectl get events --sort-by='.lastTimestamp' | tail -20
```

## Full Teardown

```bash
helm uninstall atlas
kubectl delete pvc -l app.kubernetes.io/instance=atlas   # wipes the DB
kind delete cluster
```

## Troubleshooting

**`ErrImageNeverPull`:** Image not loaded into Kind. Run `kind load docker-image atlas-web:1.0.0` again.

**`secrets.dbPassword must be set`:** Use `helm install atlas ./helm/atlas -f values-local.yaml`.

**`secrets.demoTokenSecret must be set to a 32+ byte hex string`:** Generate with `openssl rand -hex 32` and paste the output — don't use a shell command in the values file.

**Port-forward dies after rollout restart:** Expected. Run `kubectl port-forward svc/atlas-atlas-web 5050:5050` again.

**Browser stuck on "Processing...":** Check pod logs for tracebacks. If logs only show health checks, the port-forward is dead. Restart it.

**No nodes found for cluster "kind":** Run `kind create cluster --name kind` first.

## Note on Local Docker Compose

For local development (not Kubernetes), use `docker compose up -d --build` instead. See terminal_notes.md for docker compose workflow details. The Kind/Helm path above is for staging/production deploys.
