#!/usr/bin/env python3

# %%
# Importing Necessary Libraries
import hashlib
import hmac
import json
import logging
import os
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable
from xml.sax.saxutils import escape
from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse

# Define Logging Configuration
logging.basicConfig(level=logging.INFO)

# %%
# Mock Poster Service
# Behaves like a small asynchronous service:
#   - a job takes 5-20 s (status is derived from timestamps, no threads needed)
#   - about 1 in 10 jobs fails at random
#   - invalid input is rejected with a clear error
#   - requests need `Authorization: Bearer <POSTER_API_KEY>`
#   - the `Idempotency-Key` header protects against duplicate jobs on retry
#
# Run: POSTER_API_KEY=dev-key uvicorn mock_backend.app:createApp --factory --port 8000
maxPromptChars = 500
allowedResolutions = ("1k", "2k")

templateCatalog = [
    {"id": "tpl_sale_bold", "name": "Weekend Sale Bold", "category": "sale"},
    {"id": "tpl_sale_clean", "name": "Clean Discount Banner", "category": "sale"},
    {"id": "tpl_coffee_steam", "name": "Coffee Steam", "category": "food-and-beverage"},
    {"id": "tpl_burger_hero", "name": "Burger Hero Shot", "category": "food-and-beverage"},
    {"id": "tpl_dessert_pop", "name": "Dessert Pop", "category": "food-and-beverage"},
    {"id": "tpl_ramadan_glow", "name": "Ramadan Kareem Glow", "category": "occasion"},
    {"id": "tpl_national_day", "name": "Saudi National Day", "category": "occasion"},
    {"id": "tpl_new_arrival", "name": "New Arrival Spotlight", "category": "new-product"},
    {"id": "tpl_minimal_dark", "name": "Minimal Dark Product", "category": "minimal"},
]

# %%
# Settings, Job Model and Error Type
@dataclass
class backendSettings:
    """
    Configuration of the mock backend.

    Arguments:
    ----------
    apiKey : str
        The shared secret clients must send as a Bearer token.
    minSeconds : float
        Shortest job duration in seconds.
    maxSeconds : float
        Longest job duration in seconds.
    failRate : float
        Probability (0-1) that a job fails at random.
    failFirstN : int
        Force the first N jobs to fail (used by demos and evals).
    maxActiveJobs : int
        Maximum number of jobs processing at once (cost and abuse guard).
    timeScale : float
        Multiplier on job duration, e.g. 0.1 makes a 5-20 s job take 0.5-2 s.
    signingSecret : str
        Secret used to sign image links.
    publicUrl : str
        Public base URL used to build image links.
    imageLinkTtl : int
        Lifetime of image links in seconds.
    seed : int | None
        Optional random seed for repeatable runs.
    """
    apiKey: str = ""
    minSeconds: float = 5.0
    maxSeconds: float = 20.0
    failRate: float = 0.1
    failFirstN: int = 0
    maxActiveJobs: int = 5
    timeScale: float = 1.0
    signingSecret: str = field(default_factory=lambda: uuid.uuid4().hex)
    publicUrl: str = "http://localhost:8000"
    imageLinkTtl: int = 900
    seed: int | None = None

    @classmethod
    def fromEnv(cls) -> "backendSettings":
        """Build the settings from environment variables."""
        env = os.environ.get
        return cls(
            apiKey=env("POSTER_API_KEY", ""),
            minSeconds=float(env("MOCK_MIN_SECONDS", 5)),
            maxSeconds=float(env("MOCK_MAX_SECONDS", 20)),
            failRate=float(env("MOCK_FAIL_RATE", 0.1)),
            failFirstN=int(env("MOCK_FAIL_FIRST_N", 0)),
            maxActiveJobs=int(env("MOCK_MAX_ACTIVE_JOBS", 5)),
            timeScale=float(env("MOCK_TIME_SCALE", 1)),
            publicUrl=env("MOCK_PUBLIC_URL", "http://localhost:8000"),
            seed=int(env("MOCK_SEED")) if env("MOCK_SEED") else None,
        )


@dataclass
class posterJob:
    """One poster generation job. Its status is derived from the clock, so no background thread is needed."""
    id: str
    params: dict
    fingerprint: str
    created: float
    duration: float
    willFail: bool
    cancelledAt: float | None = None

    def getStatus(self, now: float) -> str:
        """Return processing, completed, failed or cancelled for the given time."""
        if self.cancelledAt is not None:
            return "cancelled"
        if now - self.created < self.duration:
            return "processing"
        return "failed" if self.willFail else "completed"


class apiError(Exception):
    """An error that is turned into a JSON error response: {"error": {"code", "message"}}."""
    def __init__(self, httpStatus: int, code: str, message: str) -> None:
        self.httpStatus, self.code, self.message = httpStatus, code, message

# %%
# Input Validation
def isValidImageRef(value: Any) -> bool:
    """Accept only https:// URLs and asset:// references."""
    return isinstance(value, str) and value.startswith(("https://", "asset://")) and len(value) > 8


def validateJobRequest(body: Any) -> dict:
    """
    Validate a create-job request body.

    Arguments:
    ----------
    body : Any
        The parsed JSON body of POST /jobs.

    Returns:
    --------
    dict
        The cleaned job parameters. Raises apiError (400 / 404) when the input is invalid.
    """
    if not isinstance(body, dict):
        raise apiError(400, "invalid_request", "Request body must be a JSON object.")
    for name in ("product_image", "background_image"):
        if not isValidImageRef(body.get(name)):
            raise apiError(400, "invalid_image", f"`{name}` is required and must be an https:// URL or an asset:// reference.")
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise apiError(400, "invalid_prompt", "`prompt` is required and must be a non-empty string.")
    if len(prompt) > maxPromptChars:
        raise apiError(400, "prompt_too_long", f"`prompt` is {len(prompt)} characters; the maximum is {maxPromptChars}.")
    imageCount = body.get("image_count", 1)
    if isinstance(imageCount, bool) or not isinstance(imageCount, int) or not 1 <= imageCount <= 4:
        raise apiError(400, "invalid_image_count", f"`image_count` is {imageCount!r}; it must be an integer from 1 to 4.")
    resolution = body.get("resolution", "1k")
    if resolution not in allowedResolutions:
        raise apiError(400, "invalid_resolution", f"`resolution` is {resolution!r}; it must be \"1k\" or \"2k\".")
    templateId = body.get("template_id")
    if templateId is not None and templateId not in {t["id"] for t in templateCatalog}:
        raise apiError(404, "template_not_found", f"Template {templateId!r} does not exist.")
    return {
        "template_id": templateId,
        "product_image": body["product_image"],
        "background_image": body["background_image"],
        "prompt": prompt,
        "image_count": imageCount,
        "resolution": resolution,
    }

# %%
# FastAPI Application Factory
def createApp(settings: backendSettings | None = None, clock: Callable[[], float] = time.monotonic) -> FastAPI:
    """
    Create the mock poster service.

    Arguments:
    ----------
    settings : backendSettings | None
        Service configuration. Read from environment variables when omitted.
    clock : Callable[[], float]
        Function returning the current time in seconds. Tests pass a fake clock so jobs finish instantly.
    """
    settings = settings or backendSettings.fromEnv()
    if not settings.apiKey:
        raise RuntimeError("POSTER_API_KEY is not set; the mock backend refuses to start without a key.")

    rng = random.Random(settings.seed)
    jobs: dict[str, posterJob] = {}
    jobsByKey: dict[str, posterJob] = {}
    createdCount = 0

    app = FastAPI(title="Mock Poster Service")
    app.state.settings, app.state.jobs = settings, jobs

    @app.exception_handler(apiError)
    async def handleApiError(_: Request, exc: apiError):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.httpStatus)

    def authenticate(authorization: str | None) -> None:
        token = (authorization or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(token.encode(), settings.apiKey.encode()):
            raise apiError(401, "unauthorized", "Missing or invalid API key.")

    def signLink(jobId: str, index: int, expires: int) -> str:
        message = f"{jobId}:{index}:{expires}".encode()
        return hmac.new(settings.signingSecret.encode(), message, hashlib.sha256).hexdigest()[:32]

    def describeJob(job: posterJob) -> dict:
        now = clock()
        status = job.getStatus(now)
        out: dict[str, Any] = {"job_id": job.id, "status": status, "elapsed_seconds": round(now - job.created, 1)}
        if status == "completed":
            expires = int(time.time()) + settings.imageLinkTtl
            out["links_expire_in_seconds"] = settings.imageLinkTtl
            out["images"] = [
                {
                    "index": i,
                    "resolution": job.params["resolution"],
                    "url": f"{settings.publicUrl}/images/{job.id}/{i}.svg?exp={expires}&sig={signLink(job.id, i, expires)}",
                }
                for i in range(1, job.params["image_count"] + 1)
            ]
        elif status == "failed":
            out["error"] = {
                "code": "generation_failed",
                "message": "The image model returned an internal error while generating this poster.",
                "retryable": True,
            }
        return out

    def getJobOr404(jobId: str) -> posterJob:
        job = jobs.get(jobId)
        if not job:
            raise apiError(404, "job_not_found", f"No job with id {jobId!r}.")
        return job

    @app.get("/templates")
    def listTemplates(category: str | None = None, search: str | None = None, authorization: str | None = Header(None)):
        authenticate(authorization)
        items = templateCatalog
        if category:
            items = [t for t in items if t["category"] == category.lower()]
        if search:
            items = [t for t in items if search.lower() in t["name"].lower()]
        return {"templates": items, "categories": sorted({t["category"] for t in templateCatalog})}

    @app.post("/jobs", status_code=202)
    async def createJob(
        request: Request,
        response: Response,
        authorization: str | None = Header(None),
        idempotencyKey: str | None = Header(None, alias="Idempotency-Key"),
    ):
        nonlocal createdCount
        authenticate(authorization)
        try:
            body = await request.json()
        except ValueError:
            raise apiError(400, "invalid_request", "Request body is not valid JSON.")
        params = validateJobRequest(body)
        fingerprint = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()

        if idempotencyKey:
            existing = jobsByKey.get(idempotencyKey)
            # A key is only "used up" by a job that is processing or completed.
            if existing and existing.getStatus(clock()) in ("processing", "completed"):
                if existing.fingerprint != fingerprint:
                    raise apiError(409, "idempotency_conflict", "This Idempotency-Key was already used with different inputs.")
                response.status_code = 200
                return {**describeJob(existing), "duplicate": True}

        activeJobs = sum(1 for j in jobs.values() if j.getStatus(clock()) == "processing")
        if activeJobs >= settings.maxActiveJobs:
            raise apiError(429, "too_many_active_jobs", f"{activeJobs} jobs are already processing (limit {settings.maxActiveJobs}).")

        createdCount += 1
        willFail = createdCount <= settings.failFirstN or rng.random() < settings.failRate
        duration = rng.uniform(settings.minSeconds, settings.maxSeconds) * settings.timeScale
        job = posterJob(uuid.uuid4().hex[:12], params, fingerprint, clock(), duration, willFail)
        jobs[job.id] = job
        if idempotencyKey:
            jobsByKey[idempotencyKey] = job
        logging.info("Job %s created (%s image(s), %s)", job.id, params["image_count"], params["resolution"])
        return {"job_id": job.id, "status": "processing", "duplicate": False}

    @app.get("/jobs/{jobId}")
    def getJob(jobId: str, authorization: str | None = Header(None)):
        authenticate(authorization)
        return describeJob(getJobOr404(jobId))

    @app.post("/jobs/{jobId}/cancel")
    def cancelJob(jobId: str, authorization: str | None = Header(None)):
        authenticate(authorization)
        job = getJobOr404(jobId)
        status = job.getStatus(clock())
        if status == "cancelled":
            return describeJob(job)
        if status != "processing":
            raise apiError(409, "job_already_finished", f"Job {jobId} already finished with status {status!r}.")
        job.cancelledAt = clock()
        return describeJob(job)

    @app.get("/images/{jobId}/{index}.svg")
    def getImage(jobId: str, index: int, exp: int, sig: str):
        job = jobs.get(jobId)
        isValid = job is not None and hmac.compare_digest(sig, signLink(jobId, index, exp))
        if not isValid or exp < time.time():
            raise apiError(403, "link_invalid_or_expired", "Image link is invalid or expired; call get_job_status for fresh links.")
        text = escape(job.params["prompt"][:60])
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="768" height="1024">'
            '<rect width="100%" height="100%" fill="#1f2937"/>'
            f'<text x="50%" y="45%" fill="#fff" font-size="32" text-anchor="middle">MOCK POSTER {index}</text>'
            f'<text x="50%" y="52%" fill="#fbbf24" font-size="22" text-anchor="middle">{text}</text>'
            "</svg>"
        )
        return Response(svg, media_type="image/svg+xml")

    return app
