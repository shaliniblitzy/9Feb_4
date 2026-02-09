# -*- coding: utf-8 -*-
"""
Gunicorn WSGI Server Configuration
===================================

Production-grade Gunicorn configuration for the Flask Reverse Document Generator
server. This file is loaded automatically by Gunicorn when referenced via the
``--config`` flag:

    gunicorn --config gunicorn.conf.py app.main:application

Configuration Highlights:
    - Optimized for long-running LangGraph workflow executions (up to 1 hour)
    - Cloud Run Service compatible (binds 0.0.0.0:8080, uses /dev/shm)
    - Memory-conscious worker count for LLM-intensive operations
    - Worker recycling via max_requests to prevent memory leaks
    - gthread worker class for concurrent request handling within workers
    - Structured logging compatible with Cloud Run log aggregation

Environment Variables:
    PORT              : Bind port (default: 8080, Cloud Run injects this)
    WEB_CONCURRENCY   : Number of Gunicorn workers (default: 2)
    GUNICORN_THREADS  : Threads per worker (default: 4)
    GUNICORN_TIMEOUT  : Worker timeout in seconds (default: 3600)
    GUNICORN_LOGLEVEL : Log level (default: info)
"""

import os
import multiprocessing

# ---------------------------------------------------------------------------
# Helper: Resolve the default worker count
# ---------------------------------------------------------------------------
# For LLM-intensive workloads (Claude Opus, GPT-5 Mini, Voyage AI embeddings),
# each worker can consume significant memory (multi-GB for large context
# windows up to 300K tokens).  We default to 2 workers rather than the
# typical (2 * cpu_count) + 1 formula to avoid OOM kills on Cloud Run
# instances.  The WEB_CONCURRENCY environment variable takes precedence so
# that the deployment environment can tune the value at runtime.

def _default_workers() -> int:
    """Return a safe default worker count based on available CPUs.

    Cloud Run surfaces the allocated vCPU count through the OS.  We cap the
    automatic calculation at ``min(cpu_count, 4)`` to prevent excessive
    memory pressure from concurrent LLM operations while still allowing
    parallelism on larger instances.

    Returns:
        int: The number of workers to spawn (minimum 1, maximum 4).
    """
    try:
        cpu_count = multiprocessing.cpu_count()
    except NotImplementedError:
        # Fallback when cpu_count is unavailable (rare, but defensive)
        cpu_count = 1
    # Use at most 4 workers to stay within memory budget; at least 1
    return max(1, min(cpu_count, 4))


# ---------------------------------------------------------------------------
# Server Socket
# ---------------------------------------------------------------------------
# Cloud Run injects the PORT environment variable (typically 8080).  Binding
# to 0.0.0.0 ensures the container accepts traffic from the Cloud Run proxy.

bind = "0.0.0.0:{port}".format(port=os.environ.get("PORT", "8080"))

# ---------------------------------------------------------------------------
# Worker Processes
# ---------------------------------------------------------------------------
# WEB_CONCURRENCY is a widely adopted convention (Heroku, Cloud Run, Railway)
# for controlling worker count at the infrastructure layer.  Default 2 for
# the memory-intensive LLM workload profile of the Reverse Document Generator.

workers = int(os.environ.get("WEB_CONCURRENCY", str(_default_workers())))

# ---------------------------------------------------------------------------
# Threads per Worker
# ---------------------------------------------------------------------------
# Each worker spawns this many threads.  With the gthread worker class, these
# threads share the worker process' memory.  4 threads per worker gives a
# concurrency of (workers * threads) = 8 concurrent requests with the default
# 2-worker setup — enough for the expected API traffic while keeping memory
# bounded.

threads = int(os.environ.get("GUNICORN_THREADS", "4"))

# ---------------------------------------------------------------------------
# Worker Class
# ---------------------------------------------------------------------------
# ``gthread`` (threaded) workers provide efficient concurrency for I/O-bound
# workloads (LLM API calls, Neo4j queries, GCS uploads) without the memory
# overhead of separate processes.  Each worker process runs multiple threads
# sharing a single Python interpreter and memory space.

worker_class = "gthread"

# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------
# The Reverse Document Generator executes LangGraph workflows that can run
# for extended periods — full GENERATE mode with 30+ sections and multiple
# LLM round-trips per section can exceed 30 minutes.  A 1-hour timeout
# (3600s) ensures that the longest plausible document generation cycle
# completes without being killed.
#
# graceful_timeout gives in-flight requests additional time to finish after
# a worker is signaled to restart (e.g., via max_requests recycling or
# SIGHUP).  300 seconds (5 minutes) allows most section-level operations
# to complete gracefully.

timeout = int(os.environ.get("GUNICORN_TIMEOUT", "3600"))
graceful_timeout = 300

# ---------------------------------------------------------------------------
# Keep-Alive
# ---------------------------------------------------------------------------
# How long to wait for requests on a Keep-Alive connection.  Cloud Run's
# internal load balancer manages connection pooling, so a short keepalive
# (5 seconds) is sufficient.  This prevents idle connections from holding
# worker threads unnecessarily.

keepalive = 5

# ---------------------------------------------------------------------------
# Worker Recycling
# ---------------------------------------------------------------------------
# Periodically restart workers after processing a set number of requests to
# mitigate potential memory leaks from long-lived LLM SDK sessions, Neo4j
# connections, or accumulated in-memory caches (e.g., attachment_base64_cache).
#
# max_requests_jitter adds randomness so workers don't all restart
# simultaneously, which would cause a brief availability gap.

max_requests = 1000
max_requests_jitter = 50

# ---------------------------------------------------------------------------
# Application Loading
# ---------------------------------------------------------------------------
# preload_app=True loads the Flask application in the master process before
# forking workers.  Benefits:
#   1. Shared memory: read-only application code and configuration are
#      shared across workers via copy-on-write.
#   2. Faster worker startup: workers don't re-import and re-initialize.
#   3. Early failure detection: import errors surface immediately at boot.
#
# Trade-off: post_fork hooks are required for any resources that must be
# per-worker (e.g., database connections), but the Flask application factory
# pattern (create_app) already handles this via app context initialization.

preload_app = True

# ---------------------------------------------------------------------------
# Temporary File Directory
# ---------------------------------------------------------------------------
# Cloud Run containers run on tmpfs-backed filesystems.  Writing Gunicorn's
# worker heartbeat files to /dev/shm (shared memory) avoids potential I/O
# bottlenecks on the container's overlay filesystem and is the recommended
# practice for containerized Gunicorn deployments.

worker_tmp_dir = "/dev/shm"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# Cloud Run captures stdout/stderr and routes them to Cloud Logging.
# Using "-" directs Gunicorn's access and error logs to stdout/stderr
# respectively, ensuring all log output is captured by the platform.
#
# The log level can be tuned at runtime via GUNICORN_LOGLEVEL without
# rebuilding the container image.

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOGLEVEL", "info")

# Use a structured access log format that includes key fields for
# observability: method, path, status, response time, and content length.
# This format is easily parseable by Cloud Logging and compatible with
# structured log aggregation pipelines.
access_log_format = (
    '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(L)s'
)

# ---------------------------------------------------------------------------
# Process Naming
# ---------------------------------------------------------------------------
# Set a descriptive process title so that `ps` and monitoring tools can
# easily identify this application's worker processes.

proc_name = "reverse-document-generator"

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
# Limit the maximum size of HTTP request headers.  The default (8190 bytes)
# is sufficient for the JSON API payloads used by this service.  This
# protects against oversized header attacks.

limit_request_line = 8190
limit_request_fields = 100
limit_request_field_size = 8190

# ---------------------------------------------------------------------------
# Server Mechanics
# ---------------------------------------------------------------------------
# Forwarded-allow-ips: Cloud Run terminates TLS and forwards requests via
# an internal proxy.  "*" trusts all forwarded headers, which is safe within
# the Cloud Run environment where the proxy is the only source of traffic.
# For non-Cloud Run deployments, this should be restricted to known proxy IPs.

forwarded_allow_ips = "*"

# Strip any incoming X-Forwarded-Proto headers and trust the Cloud Run proxy
# to set them correctly.  This prevents header spoofing from external clients.
proxy_protocol = False

# ---------------------------------------------------------------------------
# Server Hooks
# ---------------------------------------------------------------------------
# These hooks provide observability into the Gunicorn worker lifecycle.
# They log key events (boot, worker start/exit, request handling) to
# stderr, which is captured by Cloud Run's logging infrastructure.


def on_starting(server):
    """Called just before the master process is initialized.

    Logs the Gunicorn configuration summary for operational visibility.
    """
    server.log.info(
        "Gunicorn master starting — bind=%s workers=%d threads=%d "
        "timeout=%ds worker_class=%s preload=%s",
        bind,
        workers,
        threads,
        timeout,
        worker_class,
        preload_app,
    )


def post_fork(server, worker):
    """Called just after a worker has been forked.

    Logs the worker PID for process tracking and monitoring.  This is
    the appropriate hook for per-worker resource initialization if needed
    (e.g., database connection pools that cannot be shared across forks).
    """
    server.log.info(
        "Worker spawned — pid=%s worker_id=%s", worker.pid, worker.age
    )


def worker_exit(server, worker):
    """Called when a worker process exits.

    Logs the exit event for tracking worker recycling (max_requests) and
    unexpected terminations.  Useful for diagnosing memory issues or
    crash loops.
    """
    server.log.info(
        "Worker exiting — pid=%s worker_id=%s", worker.pid, worker.age
    )


def on_exit(server):
    """Called just before the master process exits.

    Provides a clean shutdown log entry for operational tracking.
    """
    server.log.info("Gunicorn master shutting down")


def worker_abort(worker):
    """Called when a worker is killed due to timeout.

    This is critical for the Reverse Document Generator — a timeout kill
    during a long-running LangGraph workflow indicates that the timeout
    setting may be too low, or that a specific document generation request
    is pathologically slow.  The log entry helps operators diagnose these
    cases.
    """
    worker.log.warning(
        "Worker ABORTED (timeout) — pid=%s worker_id=%s. "
        "Consider increasing GUNICORN_TIMEOUT if this occurs frequently "
        "during document generation workflows.",
        worker.pid,
        worker.age,
    )
