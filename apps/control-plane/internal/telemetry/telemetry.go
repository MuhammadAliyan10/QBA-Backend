package telemetry

import (
	"context"
	"fmt"
	"log"
	"os"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/prometheus"
	"go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	semconv "go.opentelemetry.io/otel/semconv/v1.24.0"
)

// InitTelemetry initializes OpenTelemetry with Prometheus metrics
func InitTelemetry(serviceName string) (*metric.MeterProvider, error) {
	// Create resource with service name
	res, err := resource.Merge(
		resource.Default(),
		resource.NewWithAttributes(
			semconv.SchemaURL,
			semconv.ServiceName(serviceName),
			semconv.ServiceVersion("1.0.0"),
		),
	)
	if err != nil {
		return nil, fmt.Errorf("failed to create resource: %w", err)
	}

	// Create Prometheus exporter
	exporter, err := prometheus.New()
	if err != nil {
		return nil, fmt.Errorf("failed to create prometheus exporter: %w", err)
	}

	// Create meter provider
	meterProvider := metric.NewMeterProvider(
		metric.WithReader(exporter),
		metric.WithResource(res),
	)

	// Set global meter provider
	otel.SetMeterProvider(meterProvider)

	log.Printf("[Telemetry] OpenTelemetry initialized for service: %s", serviceName)
	return meterProvider, nil
}

// EnableTracing returns true if tracing is enabled via environment variable
func EnableTracing() bool {
	return os.Getenv("ENABLE_TRACING") == "true"
}

// EnableMetrics returns true if metrics are enabled via environment variable
func EnableMetrics() bool {
	value := os.Getenv("ENABLE_METRICS")
	return value == "" || value == "true" // Default to true
}

// Shutdown gracefully shuts down telemetry
func Shutdown(provider *metric.MeterProvider) {
	if provider != nil {
		if err := provider.Shutdown(context.Background()); err != nil {
			log.Printf("Error shutting down meter provider: %v", err)
		}
	}
}
