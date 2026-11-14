# Nexent DSML compatibility layer

This compatibility layer translates DeepSeek textual tool calls such as:

```text
<｜DSML｜ calls>
<｜DSML｜ invoke name="read_skill_md">
<｜DSML｜ parameter name="skill_name" string="true">ontology-change-draft</｜DSML｜ parameter>
</｜DSML｜ invoke>
</｜DSML｜ calls>
```

into the Python action protocol accepted by Nexent 2.8.0:

```xml
<code>
read_skill_md(skill_name='ontology-change-draft')
</code>
```

The derived image injects normalization into both
`nexent.core.agents.core_agent.CoreAgent` and
`nexent.core.agents.output_protocol.classify_model_output`. The CoreAgent hook
covers legacy and structured parsing paths that bypass the classifier; the
classifier hook remains as a second safety boundary. The patch applies to the
main Agent and both managed Agents because they share the runtime.

## Build a durable runtime image

Run from this directory:

```bash
cd /opt/lightrag-domain/deploy/nexent_dsml_compat

docker build \
  -t nexent-dsml-compat:v2.8.0 \
  .
```

Verify the image contains the patch:

```bash
docker run --rm \
  nexent-dsml-compat:v2.8.0 \
  python -c "import nexent_dsml_compat as c; print(c.COMPAT_VERSION)"
```

Expected output:

```text
2.2.0
```

## Start the runtime with the patched image

The compose override changes only `nexent-runtime`:

```bash
cd /root/project/nexent/deploy/docker/compose

export APP_VERSION=v2.8.0
set -a
source ../../env/.env
source ../../env/image-source.general.env
set +a

docker compose \
  -p nexent \
  -f docker-compose.yml \
  -f /opt/lightrag-domain/deploy/nexent_dsml_compat/docker-compose.override.yml \
  up -d --force-recreate nexent-runtime
```

Verify:

```bash
docker inspect nexent-runtime --format '{{.Config.Image}}'
docker logs --tail 100 nexent-runtime 2>&1
```

Expected image:

```text
nexent-dsml-compat:v2.8.0
```

Start a new Nexent conversation and request a governance read. The output
should contain a real tool call rather than a visible `<｜DSML｜>` block.

## Roll back

Recreate the runtime with the original compose file and image:

```bash
cd /root/project/nexent/deploy/docker/compose

export APP_VERSION=v2.8.0
set -a
source ../../env/.env
source ../../env/image-source.general.env
set +a

docker compose \
  -p nexent \
  -f docker-compose.yml \
  up -d --force-recreate nexent-runtime
```

## Safety boundaries

- Tool names must match a Python identifier.
- Parameters are decoded with `json.loads`/`ast.literal_eval`; the patch never
  calls `eval`.
- Unknown DSML shapes are left unchanged, allowing Nexent's normal protocol
  diagnostics to run.
- The patch changes only the model-output boundary. It does not modify MCP,
  Skills, Agent permissions, evidence policies, or governance state.
