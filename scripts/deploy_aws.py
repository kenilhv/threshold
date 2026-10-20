"""Apply the AWS shape, prove it, and tear it down.

    python scripts/deploy_aws.py plan      # what `up` would create; creates nothing
    python scripts/deploy_aws.py up        # build, push, apply, migrate, seed, wait for the API
    python scripts/deploy_aws.py verify    # submit invoices, measure from inside the VPC
    python scripts/deploy_aws.py down      # destroy everything `up` created

Needs AWS credentials in the environment or ~/.aws (never on a command line),
Docker with the daemon running, this project installed with `.[dev,fixtures]`
(the verification renders invoices with reportlab), and either a `terraform`
binary or the pinned image the Makefile already uses. Everything `up` creates,
`down` destroys, and both ask before they act unless --yes is given.

Nothing is spent before the image is known to build: `up` builds it first,
then applies. It applies twice on purpose. The first apply creates everything
with zero service tasks, so the schema can be migrated and seeded by one-shot
tasks before any long-running process asks the database for it. The second
apply raises the desired counts. A single apply would start the API against an
empty database, fail readiness, trip the circuit breaker, and leave a
half-rolled-back service to explain.

The load balancer admits only the machine running this script, by default
(--ingress auto). The ingest endpoint takes no credential, so a staging stack
open to the world is a free upload service for anyone who finds the hostname.
--ingress open is the deliberate exception.

`verify` is the measurement. It submits invoices from this machine to the load
balancer and records accept latency as the client saw it, then runs
scripts/aws_verify.py inside the VPC on the owner-credentialled task definition,
scoped to exactly the documents it submitted, because the database is reachable
from nowhere else. The two halves are written to docs/measurements/ with the
conditions they were taken under, read back from the deployment rather than
assumed.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

import boto3
import httpx
from botocore.exceptions import WaiterError

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra" / "aws"
MEASUREMENTS = ROOT / "docs" / "measurements"
TERRAFORM_IMAGE = "hashicorp/terraform:1.9"
AWS_ENVIRONMENT = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
)
ONE_SHOT_TIMEOUT = 20 * 60
API_HEALTHY_TIMEOUT = 15 * 60
# ECS caps a task override at 8 KiB; 150 ids is about 5.6 KiB with the rest of
# the command. Enough for a measurement, and the cap keeps it from failing late.
MAX_VERIFY_COUNT = 150
# The verifier scopes by document id, so this only widens the fallback window;
# generous because a laptop clock ahead of RDS would otherwise hide the run.
SINCE_MARGIN = timedelta(seconds=60)
BILLING_REMINDER = "billable resources exist; run `python scripts/deploy_aws.py down` to stop them"


def say(message: str) -> None:
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def fail(message: str) -> NoReturn:
    sys.stderr.write(f"error: {message}\n")
    raise SystemExit(1)


def git_tag() -> str:
    """The commit, so the image a task runs is the code in the log."""
    sha = subprocess.run(
        ["git", "rev-parse", "--short=12", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", "src", "migrations", "scripts", "seeds"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return f"{sha}-dirty-{datetime.now(UTC):%Y%m%d%H%M%S}" if dirty else sha


def my_public_address() -> str:
    """The address the load balancer should admit, as the internet sees this machine."""
    try:
        address = httpx.get("https://checkip.amazonaws.com", timeout=10.0).text.strip()
    except httpx.HTTPError as exc:
        fail(f"could not learn this machine's public address ({exc}); pass --ingress explicitly")
    if not address or any(c not in "0123456789." for c in address):
        fail(f"unexpected answer from checkip: {address!r}; pass --ingress explicitly")
    return f"{address}/32"


def ingress_cidrs(choice: str) -> list[str]:
    if choice == "auto":
        return [my_public_address()]
    if choice == "open":
        return ["0.0.0.0/0"]
    return [part.strip() for part in choice.split(",") if part.strip()]


# ----------------------------------------------------------------- terraform --


class Terraform:
    """`terraform` on the PATH if there is one, else the pinned image."""

    def __init__(self, variables: dict[str, Any]) -> None:
        self.variables = variables
        self.local = shutil.which("terraform") is not None

    def _command(self, *args: str) -> list[str]:
        if self.local:
            return ["terraform", f"-chdir={INFRA}", *args]
        command = [
            "docker",
            "run",
            "--rm",
            "-i",
            "-v",
            f"{INFRA}:/w",
            "-w",
            "/w",
            "-e",
            "TF_IN_AUTOMATION=1",
        ]
        for name in AWS_ENVIRONMENT:
            if name in os.environ:
                command += ["-e", name]
        aws_dir = Path.home() / ".aws"
        if aws_dir.is_dir():
            command += ["-v", f"{aws_dir}:/root/.aws:ro"]
        return [*command, TERRAFORM_IMAGE, *args]

    def _var_args(self) -> list[str]:
        args = []
        for key, value in self.variables.items():
            rendered = json.dumps(value) if isinstance(value, list) else str(value)
            args.append(f"-var={key}={rendered}")
        tfvars = INFRA / f"{self.variables['environment']}.tfvars"
        if tfvars.exists():
            args.append(f"-var-file={tfvars.name}")
        return args

    def run(self, *args: str, capture: bool = False) -> str:
        command = self._command(*args)
        say(f"$ terraform {' '.join(a for a in args if not a.startswith('-var='))}")
        completed = subprocess.run(command, cwd=ROOT, capture_output=capture, text=True)
        if completed.returncode != 0:
            if capture:
                sys.stderr.write(completed.stderr)
            fail(f"terraform {args[0]} exited {completed.returncode}")
        return completed.stdout if capture else ""

    def init(self) -> None:
        self.run("init", "-input=false", "-no-color")

    def plan(self) -> None:
        self.run("plan", "-input=false", "-no-color", *self._var_args())

    def apply(self) -> None:
        self.run("apply", "-input=false", "-no-color", "-auto-approve", *self._var_args())

    def destroy(self) -> None:
        self.run("destroy", "-input=false", "-no-color", "-auto-approve", *self._var_args())

    def outputs(self) -> dict[str, Any]:
        raw = self.run("output", "-json", "-no-color", capture=True)
        return {k: v["value"] for k, v in json.loads(raw).items()}


# ------------------------------------------------------------------- helpers --


def preflight(region: str, *, need_docker: bool = True) -> dict[str, str]:
    session = boto3.session.Session(region_name=region)
    try:
        identity = session.client("sts").get_caller_identity()
    except Exception as exc:  # whatever boto3 raises, the answer is the same
        fail(
            "no usable AWS credentials. Configure them yourself (aws configure, or "
            "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in the environment) "
            f"and run this again: {exc}"
        )
    if need_docker:
        if shutil.which("docker") is None:
            fail("docker is not on the PATH")
        daemon = subprocess.run(["docker", "info"], capture_output=True, text=True)
        if daemon.returncode != 0:
            fail("the Docker daemon is not running; start Docker Desktop and run this again")
    say(f"account ...{identity['Account'][-4:]} as {identity['Arn']} in {region}")
    return {"account": identity["Account"], "arn": identity["Arn"]}


def confirm(prompt: str, assume_yes: bool) -> None:
    if assume_yes:
        return
    answer = input(f"{prompt} [type yes] ").strip().lower()
    if answer != "yes":
        fail("stopped; nothing was changed by this step")


def build_image(tag: str) -> str:
    """Build the runtime stage for Fargate's architecture under a local name."""
    local = f"threshold:{tag}"
    say(f"$ docker build --platform linux/amd64 --target runtime -t {local} .")
    subprocess.run(
        ["docker", "build", "--platform", "linux/amd64", "--target", "runtime", "-t", local, "."],
        check=True,
        cwd=ROOT,
    )
    return local


def push_image(session: boto3.session.Session, local: str, repository_url: str, tag: str) -> str:
    ecr = session.client("ecr")
    auth = ecr.get_authorization_token()["authorizationData"][0]
    user, token = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
    registry = auth["proxyEndpoint"]
    image = f"{repository_url}:{tag}"
    say(f"$ docker login {registry}")
    subprocess.run(
        ["docker", "login", "--username", user, "--password-stdin", registry],
        input=token.encode(),
        check=True,
        cwd=ROOT,
    )
    subprocess.run(["docker", "tag", local, image], check=True, cwd=ROOT)
    say(f"$ docker push {image}")
    subprocess.run(["docker", "push", image], check=True, cwd=ROOT)
    return image


def log_lines(logs: Any, group: str, stream: str) -> list[str]:
    lines: list[str] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "logGroupName": group,
            "logStreamName": stream,
            "startFromHead": True,
        }
        if token:
            kwargs["nextToken"] = token
        try:
            page = logs.get_log_events(**kwargs)
        except logs.exceptions.ResourceNotFoundException:
            return lines
        lines.extend(event["message"] for event in page["events"])
        if page.get("nextForwardToken") in (None, token):
            return lines
        token = page["nextForwardToken"]


def run_admin_task(
    session: boto3.session.Session,
    out: dict[str, Any],
    command: list[str],
    label: str,
    *,
    max_wait: int = ONE_SHOT_TIMEOUT,
    until_line_starts_with: str | None = None,
) -> tuple[int | None, list[str]]:
    """Run one command on the owner-credentialled task definition and wait for it.

    CloudWatch shows a container's last lines a few seconds after the task
    stops; when a marker line is expected, the log is re-read until it appears.
    """
    ecs = session.client("ecs")
    logs = session.client("logs")
    say(f"-- {label}: {' '.join(command[:4])}{' ...' if len(command) > 4 else ''}")
    response = ecs.run_task(
        cluster=out["cluster_name"],
        taskDefinition=out["admin_task_definition"],
        launchType="FARGATE",
        count=1,
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": out["private_subnet_ids"],
                "securityGroups": [out["tasks_security_group_id"]],
                "assignPublicIp": "DISABLED",
            }
        },
        overrides={
            "containerOverrides": [{"name": out["admin_container_name"], "command": command}]
        },
    )
    if response.get("failures"):
        fail(f"{label} could not be started: {response['failures']}")
    arn = response["tasks"][0]["taskArn"]
    task_id = arn.rsplit("/", 1)[-1]
    stream = f"threshold/{out['admin_container_name']}/{task_id}"
    say(f"   task {task_id} started; waiting (log stream {stream})")
    try:
        ecs.get_waiter("tasks_stopped").wait(
            cluster=out["cluster_name"],
            tasks=[arn],
            WaiterConfig={"Delay": 10, "MaxAttempts": max(6, max_wait // 10)},
        )
    except WaiterError as exc:
        fail(f"{label} did not stop within {max_wait}s ({exc}); see {stream} in {out['log_group']}")
    task = ecs.describe_tasks(cluster=out["cluster_name"], tasks=[arn])["tasks"][0]
    container = task["containers"][0]
    exit_code = container.get("exitCode")
    lines = log_lines(logs, out["log_group"], stream)
    if until_line_starts_with:
        for _ in range(12):
            if any(line.startswith(until_line_starts_with) for line in lines):
                break
            time.sleep(5)
            lines = log_lines(logs, out["log_group"], stream)
    for line in lines[-40:]:
        say(f"   | {line}")
    say(f"   exit {exit_code}; {task.get('stoppedReason', '')}")
    return exit_code, lines


def wait_for_api(session: boto3.session.Session, out: dict[str, Any]) -> dict[str, Any]:
    elbv2 = session.client("elbv2")
    started = time.monotonic()
    while True:
        health = elbv2.describe_target_health(TargetGroupArn=out["target_group_arn"])
        states = [t["TargetHealth"]["State"] for t in health["TargetHealthDescriptions"]]
        say(f"   targets: {states or 'none registered yet'}")
        if "healthy" in states:
            break
        if time.monotonic() - started > API_HEALTHY_TIMEOUT:
            fail(
                "no healthy API target after 15 minutes; see the api log stream in "
                f"{out['log_group']}. {BILLING_REMINDER}"
            )
        time.sleep(15)
    try:
        ready = httpx.get(f"{out['api_url']}/readyz", timeout=15.0)
    except httpx.HTTPError as exc:
        fail(f"the target is healthy but {out['api_url']}/readyz is unreachable from here: {exc}")
    say(f"   GET /readyz -> {ready.status_code}")
    return (
        ready.json() if ready.headers.get("content-type", "").startswith("application/json") else {}
    )


# ------------------------------------------------------------------ commands --


def variables(args: argparse.Namespace, tag: str, counts: tuple[int, int] | None) -> dict[str, Any]:
    values: dict[str, Any] = {
        "environment": args.environment,
        "region": args.region,
        "image_tag": tag,
        "ingress_cidrs": ingress_cidrs(args.ingress),
    }
    if counts is not None:
        values["api_desired_count"] = counts[0]
        values["worker_desired_count"] = counts[1]
    return values


def cmd_plan(args: argparse.Namespace) -> None:
    preflight(args.region, need_docker=False)
    tf = Terraform(variables(args, args.tag or git_tag(), None))
    say(f"load balancer admits: {tf.variables['ingress_cidrs']}")
    tf.init()
    tf.plan()


def cmd_up(args: argparse.Namespace) -> None:
    session = boto3.session.Session(region_name=args.region)
    preflight(args.region)
    tag = args.tag or git_tag()
    say(f"image tag {tag}")

    say("\n== 1/6 build the image (nothing is billed until the next step)")
    local = build_image(tag)

    say("\n== 2/6 apply with no service tasks")
    tf = Terraform(variables(args, tag, (0, 0)))
    say(f"load balancer will admit: {tf.variables['ingress_cidrs']}")
    confirm(
        "This creates billable resources (RDS, ALB, VPC endpoints, Fargate). Continue?", args.yes
    )
    tf.init()
    tf.apply()
    out = tf.outputs()

    say("\n== 3/6 push the image")
    try:
        push_image(session, local, out["ecr_repository_url"], tag)
    except subprocess.CalledProcessError as exc:
        fail(f"push failed ({exc}). {BILLING_REMINDER}")

    say("\n== 4/6 migrate")
    code, _ = run_admin_task(session, out, ["alembic", "upgrade", "head"], "migrate")
    if code != 0:
        fail(f"migration failed; no service was started. {BILLING_REMINDER}")

    say("\n== 5/6 seed")
    code, _ = run_admin_task(session, out, ["python", "-m", "scripts.seed"], "seed")
    if code != 0:
        fail(f"seed failed. {BILLING_REMINDER}")

    say("\n== 6/6 start the services and wait for the API")
    tf = Terraform(variables(args, tag, (args.api_count, args.worker_count)))
    tf.apply()
    readiness = wait_for_api(session, out)
    say("\nreadiness: " + json.dumps(readiness, indent=2))
    say(f"\nAPI: {out['api_url']}  (admitted: {tf.variables['ingress_cidrs']})")
    say(
        "next: python scripts/deploy_aws.py verify   "
        "(and `down` when you are finished; it is billing until then)"
    )


async def submit_all(url: str, count: int, concurrency: int) -> list[tuple[int, float, str | None]]:
    """Submit `count` distinct invoices; return (status, seconds, document_id) each."""
    sys.path.insert(0, str(ROOT))
    from scripts.load_test import render_invoice  # needs ROOT on sys.path first

    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    gate = asyncio.Semaphore(concurrency)
    results: list[tuple[int, float, str | None]] = []
    with tempfile.TemporaryDirectory() as scratch:
        payloads = [
            render_invoice(Path(scratch) / f"{i}.pdf", f"AWS-{stamp}-{i:04d}") for i in range(count)
        ]

    async def one(client: httpx.AsyncClient, payload: bytes) -> None:
        async with gate:
            started = time.perf_counter()
            document_id: str | None = None
            try:
                response = await client.post(
                    f"{url}/v1/documents",
                    files={"file": ("invoice.pdf", payload, "application/pdf")},
                    timeout=60.0,
                )
                status = response.status_code
                if status in (200, 202):
                    document_id = response.json().get("document_id")
            except (httpx.HTTPError, ValueError):
                status = 0
            results.append((status, time.perf_counter() - started, document_id))

    async with httpx.AsyncClient() as client:
        await asyncio.gather(*(one(client, p) for p in payloads))
    return results


def cmd_verify(args: argparse.Namespace) -> None:
    if args.count > MAX_VERIFY_COUNT:
        fail(f"--count is capped at {MAX_VERIFY_COUNT}: the ids travel in a task override")
    session = boto3.session.Session(region_name=args.region)
    preflight(args.region, need_docker=False)
    sys.path.insert(0, str(ROOT))
    try:
        from scripts.load_test import render_invoice, summarise  # noqa: F401
    except ImportError as exc:
        fail(f"verification renders invoices and needs `pip install -e .[dev,fixtures]`: {exc}")

    tf = Terraform(variables(args, args.tag or "latest", None))
    out = tf.outputs()
    url = out["api_url"]

    say(f"== submitting {args.count} invoices at concurrency {args.concurrency} to {url}")
    since = datetime.now(UTC) - SINCE_MARGIN
    wall_started = time.perf_counter()
    results = asyncio.run(submit_all(url, args.count, args.concurrency))
    wall = time.perf_counter() - wall_started
    accepted = [d for s, _, d in results if s == 202 and d]
    duplicates = [d for s, _, d in results if s == 200]
    errors = [s for s, _, _ in results if s not in (200, 202)]
    latencies = [seconds for status, seconds, _ in results if status in (200, 202)]
    say(f"   202 x{len(accepted)}  200 x{len(duplicates)}  other x{len(errors)}  in {wall:.2f}s")
    if not accepted:
        fail("nothing was accepted; is the load balancer admitting this address? (--ingress)")

    say("== measuring from inside the VPC")
    code, lines = run_admin_task(
        session,
        out,
        [
            "python",
            "-m",
            "scripts.aws_verify",
            "--document-ids",
            ",".join(accepted),
            "--since",
            since.isoformat(),
            "--expect",
            str(len(accepted)),
            "--timeout",
            str(args.timeout),
        ],
        "verify",
        max_wait=args.timeout + 300,
        until_line_starts_with="AWS_VERIFY_RESULT ",
    )
    marker = next((line for line in reversed(lines) if line.startswith("AWS_VERIFY_RESULT ")), None)
    if marker is None:
        fail(f"verify task exited {code} without a result line")
    pipeline = json.loads(marker.split(" ", 1)[1])

    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True
    ).stdout.strip()
    record = {
        "measured_at": datetime.now(UTC).isoformat(),
        "conditions": {
            "environment": args.environment,
            "region": args.region,
            "commit": commit,
            "image": out["image"],
            "database_instance_class": out["database_instance_class"],
            "services": out["service_sizes"],
            # The operator's own address is personal data in a public repository;
            # the record says who was admitted without saying where they were.
            "ingress": (
                "the measuring machine's /32 only (address not recorded)"
                if all(c.endswith("/32") for c in out["ingress_cidrs"])
                else out["ingress_cidrs"]
            ),
            "postgres": pipeline.get("postgres"),
            "schema_revision": pipeline.get("schema_revision"),
            "workers_seen": pipeline.get("workers_seen"),
            "client": "this machine, over the public internet, to the application load balancer",
            "identity_provider": "none configured; authenticated routes answer 503 by design",
            "invoice_layout": "continental_tabular, one layout, unique invoice numbers",
        },
        "submit": {
            "count": args.count,
            "concurrency": args.concurrency,
            "accepted_202": len(accepted),
            "duplicate_200": len(duplicates),
            "errors": len(errors),
            "wall_seconds": round(wall, 2),
            "submits_per_second": round(len(results) / wall, 2) if wall else None,
            "accept_latency": summarise("accept", latencies),
        },
        "pipeline": pipeline,
    }
    MEASUREMENTS.mkdir(parents=True, exist_ok=True)
    target = MEASUREMENTS / f"aws-{args.environment}-{datetime.now(UTC):%Y%m%d-%H%M}.json"
    target.write_text(json.dumps(record, indent=2, default=str) + "\n", encoding="utf-8")

    say("\n== result")
    accept = record["submit"]["accept_latency"]
    settle = pipeline.get("settle", {})
    say(
        f"accept  p50 {accept.get('p50_ms')} ms  p95 {accept.get('p95_ms')} ms  "
        f"p99 {accept.get('p99_ms')} ms"
    )
    say(
        f"settle  p50 {settle.get('p50_ms')} ms  p95 {settle.get('p95_ms')} ms  "
        f"(n={settle.get('n')})"
    )
    say(
        f"matched {pipeline.get('matched')}/{len(accepted)}  "
        f"dispositions {pipeline.get('dispositions')}"
    )
    say(f"payments {pipeline.get('payments')}  dead letters {pipeline.get('dead_letters')}")
    say(f"trial balance balanced: {pipeline.get('trial_balance', {}).get('balanced')}")
    say(f"written: {target.relative_to(ROOT)}")


def cmd_down(args: argparse.Namespace) -> None:
    session = boto3.session.Session(region_name=args.region)
    preflight(args.region, need_docker=shutil.which("terraform") is None)
    tf = Terraform(variables(args, args.tag or "latest", None))
    tf.init()
    out = tf.outputs()
    if not out:
        fail(
            "no state here, so nothing can be destroyed from here. If resources exist, "
            f"they are tagged Project=threshold, Environment={args.environment}; "
            "delete them by hand or run this from the machine that ran `up`"
        )
    confirm(f"Destroy every resource of {args.environment} in {args.region}?", args.yes)
    tf.destroy()
    # Container Insights creates its own log group outside Terraform's knowledge.
    logs = session.client("logs")
    group = f"/aws/ecs/containerinsights/{out['cluster_name']}/performance"
    try:
        logs.delete_log_group(logGroupName=group)
        say(f"deleted {group}")
    except logs.exceptions.ResourceNotFoundException:
        pass
    say(
        "done. Check the bill tomorrow: Cost Explorer lags by a day, "
        "and this is the step that stops the meter."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["plan", "up", "verify", "down"])
    parser.add_argument("--environment", default="staging", choices=["staging", "production"])
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "eu-west-1",
    )
    parser.add_argument("--tag", default=None, help="image tag; defaults to the git commit")
    parser.add_argument(
        "--ingress",
        default="auto",
        help="who the load balancer admits: auto (this machine's /32), open (0.0.0.0/0), "
        "or a comma-separated list of CIDRs",
    )
    parser.add_argument("--api-count", type=int, default=2)
    parser.add_argument("--worker-count", type=int, default=1)
    parser.add_argument("--count", type=int, default=60, help="verify: invoices to submit")
    parser.add_argument("--concurrency", type=int, default=8, help="verify: submissions in flight")
    parser.add_argument(
        "--timeout", type=int, default=900, help="verify: seconds to wait for the pipeline"
    )
    parser.add_argument(
        "--yes", action="store_true", help="do not ask before creating or destroying"
    )
    args = parser.parse_args()
    {"plan": cmd_plan, "up": cmd_up, "verify": cmd_verify, "down": cmd_down}[args.command](args)


if __name__ == "__main__":
    main()
