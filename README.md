# flamegraph-viewer

Online flamegraph viewer for compressed py-spy traces.

Given a URL that points to a `.raw.gz` py-spy profile, the service downloads,
decompresses, and converts it to an interactive SVG flamegraph using
[inferno-flamegraph](https://github.com/jonhoo/inferno) — the same library
used by py-spy internally.

## Usage

```
GET https://<service-url>/?url=<encoded-profile-url>
```

**Example**

```
https://my-app-id.region.run.app/?url=https://github.com/probabl-ai/scikit-learn-benchmarks/raw/refs/heads/refactor/results/profiles/sklearn_ExtraTreesClassifier_make_trees_clsf_data_0947b_20260702T202049625116Z.raw.gz
```

The response is an `image/svg+xml` document that can be opened directly in a
browser or embedded in an `<img>` / `<object>` tag.

### Health check

```
GET /healthz  →  200 OK
```

---

## Local development

### Prerequisites

- Python 3.12+
- Rust / `cargo` (to build `inferno-flamegraph`)

### Install inferno-flamegraph

```bash
cargo install inferno
```

### Run the app

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
flask --app app run --port 8080
```

Then open:

```
http://localhost:8080/?url=https://...
```

## JSON viewer

The service can also proxy JSON and `.json.gz` files, including GitHub
LFS-backed raw files, and return them with JSON-friendly headers:

```
GET https://<service-url>/json?url=<encoded-json-url>
```

**Example**

```
https://my-app-id.region.run.app/json?url=https://github.com/probabl-ai/scikit-learn-benchmarks/raw/refs/heads/refactor/results/records/sklearn_ExtraTreesClassifier_make_trees_clsf_data_0947b_20260702T202049625116Z.json
```

The response validates that the downloaded content is valid JSON, transparently
decompresses gzip input when needed, and returns `Content-Type:
application/json`.

---

## Deployment to GCP Cloud Run

### Prerequisites

| Tool | Install |
|------|---------|
| [Docker](https://docs.docker.com/get-docker/) | `brew install docker` / [docs.docker.com](https://docs.docker.com/get-docker/) |
| [Google Cloud SDK (`gcloud`)](https://cloud.google.com/sdk/docs/install) | `brew install google-cloud-sdk` |
| A GCP project with billing enabled | [console.cloud.google.com](https://console.cloud.google.com) |

### 1 — Authenticate and set your project

```bash
gcloud auth login
gcloud config set project <YOUR_PROJECT_ID>
```

### 2 — Enable required APIs

```bash
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com
```

### 3 — Create an Artifact Registry repository (once)

```bash
gcloud artifacts repositories create flamegraph-viewer \
  --repository-format=docker \
  --location=europe-west1 \
  --description="flamegraph-viewer images"
```


### 4 — Configure Docker to use Artifact Registry

```bash
gcloud auth configure-docker europe-west1-docker.pkg.dev
```

### 5 — Build and push the image

```bash
export PROJECT_ID=$(gcloud config get-value project)
export REGION=europe-west1
export IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/flamegraph-viewer/app"

# Build (multi-stage: compiles inferno from Rust source — takes ~5 min first time)
docker build -t "${IMAGE}:latest" .

# Push
docker push "${IMAGE}:latest"
```

Alternatively, use Cloud Build to build remotely (no local Docker required):

```bash
gcloud builds submit --tag "${IMAGE}:latest"
```

### 6 — Deploy to Cloud Run

```bash
gcloud run deploy flamegraph-viewer \
  --image "${IMAGE}:latest" \
  --region "${REGION}" \
  --platform managed \
  --allow-unauthenticated \
  --port 8080 \
  --cpu 1 \
  --memory 512Mi \
  --timeout 180 \
  --concurrency 2 \
  --min-instances 0 \
  --max-instances 1 \
  --cpu-throttling
```

Cloud Run will print the service URL on success:

```
Service URL: https://flamegraph-viewer-<hash>-<region-code>.a.run.app
```

### 7 — Test the deployment

```bash
SERVICE_URL=$(gcloud run services describe flamegraph-viewer \
  --region "${REGION}" --format 'value(status.url)')

curl -o flamegraph.svg \
  "${SERVICE_URL}/?url=https://github.com/probabl-ai/scikit-learn-benchmarks/raw/refs/heads/refactor/results/profiles/sklearn_ExtraTreesClassifier_make_trees_clsf_data_0947b_20260702T202049625116Z.raw.gz"

open flamegraph.svg   # macOS — or xdg-open flamegraph.svg on Linux
```

### Continuous deployment (optional)

To redeploy automatically on every push to `main`, add a Cloud Build trigger:

```bash
gcloud builds triggers create github \
  --repo-name=flamegraph-viewer \
  --repo-owner=<GITHUB_ORG_OR_USER> \
  --branch-pattern="^main$" \
  --build-config=cloudbuild.yaml
```

A minimal `cloudbuild.yaml` is shown below — create it alongside the
`Dockerfile` if you want this workflow:

```yaml
steps:
  - name: gcr.io/cloud-builders/docker
    args: [build, -t, "$_IMAGE:$SHORT_SHA", .]
  - name: gcr.io/cloud-builders/docker
    args: [push, "$_IMAGE:$SHORT_SHA"]
  - name: gcr.io/google.com/cloudsdktool/cloud-sdk
    entrypoint: gcloud
    args:
      - run
      - deploy
      - flamegraph-viewer
      - --image=$_IMAGE:$SHORT_SHA
      - --region=$_REGION
      - --platform=managed
substitutions:
  _IMAGE: europe-west1-docker.pkg.dev/<PROJECT_ID>/flamegraph-viewer/app
  _REGION: europe-west1
```

---

## Architecture

```
Browser / curl
    │
    │  GET /?url=https://.../profile.raw.gz
    ▼
Flask app (gunicorn, 2 workers)
    │
    ├─ requests.get(url)          — download compressed profile
    ├─ gzip.decompress(data)      — decompress to folded-stack format
    └─ inferno-flamegraph (stdin) — render SVG
    │
    └─ Response: image/svg+xml
```

`inferno-flamegraph` reads the
[folded stack format](https://github.com/brendangregg/FlameGraph#2-fold-stacks)
that py-spy writes when `--format raw` is used, and produces a self-contained
interactive SVG.

---

## Security note

This service fetches arbitrary URLs supplied by the caller. Deploy it in a
trusted context (e.g. behind IAP or with
`--no-allow-unauthenticated` + a service account) if you want to restrict
access or prevent SSRF in sensitive network environments.
