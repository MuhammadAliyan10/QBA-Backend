"""
OpenTelemetry Telemetry Configuration for Execution Plane

This module initializes OpenTelemetry for distributed tracing and metrics.
It integrates with Temporal to provide end-to-end observability.
"""

import os
import logging
from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION

logger = logging.getLogger(__name__)


def init_telemetry(service_name: str = "execution-plane"):
    """
    Initialize OpenTelemetry with OTLP exporters.

    Args:
        service_name: Name of the service for telemetry
    """
    # Check if telemetry is enabled
    if not _is_enabled():
        logger.info("OpenTelemetry disabled via environment variables")
        return None, None

    # Create resource
    resource = Resource(attributes={
        SERVICE_NAME: service_name,
        SERVICE_VERSION: "1.0.0"
    })

    # Initialize tracing
    tracer_provider = None
    if _tracing_enabled():
        tracer_provider = _init_tracing(resource)
        logger.info(f"[Telemetry] OpenTelemetry tracing initialized for {service_name}")

    # Initialize metrics
    meter_provider = None
    if _metrics_enabled():
        meter_provider = _init_metrics(resource)
        logger.info(f"[Telemetry] OpenTelemetry metrics initialized for {service_name}")

    return tracer_provider, meter_provider


def _init_tracing(resource: Resource) -> TracerProvider:
    """Initialize tracing with OTLP exporter."""
    # Get OTLP endpoint from environment
    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")

    # Create OTLP span exporter
    span_exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)

    # Create tracer provider
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))

    # Set as global tracer provider
    trace.set_tracer_provider(tracer_provider)

    return tracer_provider


def _init_metrics(resource: Resource) -> MeterProvider:
    """Initialize metrics with OTLP exporter."""
    # Get OTLP endpoint from environment
    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")

    # Create OTLP metric exporter
    metric_exporter = OTLPMetricExporter(endpoint=otlp_endpoint, insecure=True)

    # Create metric reader
    metric_reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=60000)

    # Create meter provider
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])

    # Set as global meter provider
    metrics.set_meter_provider(meter_provider)

    return meter_provider


def _is_enabled() -> bool:
    """Check if telemetry is enabled."""
    return _tracing_enabled() or _metrics_enabled()


def _tracing_enabled() -> bool:
    """Check if tracing is enabled."""
    return os.getenv("ENABLE_TRACING", "false").lower() == "true"


def _metrics_enabled() -> bool:
    """Check if metrics are enabled."""
    return os.getenv("ENABLE_METRICS", "true").lower() == "true"


def shutdown_telemetry(tracer_provider: TracerProvider = None, meter_provider: MeterProvider = None):
    """Gracefully shutdown telemetry providers."""
    if tracer_provider:
        tracer_provider.shutdown()
        logger.info("Tracer provider shut down")

    if meter_provider:
        meter_provider.shutdown()
        logger.info("Meter provider shut down")
