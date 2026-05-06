package services

import (
	"encoding/csv"
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
	"strings"

	"e2e-platform/apps/control-plane/internal/db"
	"e2e-platform/apps/control-plane/internal/models"
)

// ExporterService handles data extraction and formatting for job results.
type ExporterService struct{}

// NewExporterService creates a new exporter service.
func NewExporterService() *ExporterService {
	return &ExporterService{}
}

// toSnakeCase converts a human-readable label or title into safe snake_case.
func toSnakeCase(str string) string {
	// e.g. "Extract Account Balance" -> "extract_account_balance"
	str = strings.ToLower(str)
	reg := regexp.MustCompile("[^a-z0-9]+")
	str = reg.ReplaceAllString(str, "_")
	return strings.Trim(str, "_")
}

// ReduceJobData aggregates all extracted node payloads for a given job into
// a single, flattened JSON object. The keys are the human-readable intent labels
// cleanly formatted into snake_case.
func (s *ExporterService) ReduceJobData(jobID string) (map[string]interface{}, error) {
	var logs []models.JobLog
	// Sorted sequentially so later extractions safely override earlier duplicates
	err := db.DB.Where("job_id = ? AND metadata IS NOT NULL", jobID).Order("timestamp ASC").Find(&logs).Error
	if err != nil {
		return nil, fmt.Errorf("failed to fetch job logs: %v", err)
	}

	if len(logs) == 0 {
		return make(map[string]interface{}), nil
	}

	result := make(map[string]interface{})

	for _, logEntry := range logs {
		var meta map[string]interface{}
		if err := json.Unmarshal(*logEntry.Metadata, &meta); err != nil {
			continue // skip malformed JSON
		}

		// The new Python worker pushes: {"type": "...", "content": <val>, "confidence": <float>}
		content, hasContent := meta["content"]
		if !hasContent {
			continue
		}

		// Use the Step Activity Message as the key
		snakeKey := toSnakeCase(logEntry.Message)

		// Overridable — if an extraction failed, it may send null or missing content.
		// A successfully extracted later retry can overwrite the nil.
		if content != nil || result[snakeKey] == nil {
			result[snakeKey] = content
		}
	}

	return result, nil
}

// ExportToJSON generates a flat JSON string embedding the aggregated Reducer data.
func (s *ExporterService) ExportToJSON(jobID string) (string, error) {
	reducerMap, err := s.ReduceJobData(jobID)
	if err != nil {
		return "", err
	}

	jsonBytes, err := json.MarshalIndent(reducerMap, "", "  ")
	if err != nil {
		return "", err
	}

	return string(jsonBytes), nil
}

// ExportToCSV safely converts the flattened reducer into a CSV string.
func (s *ExporterService) ExportToCSV(jobID string) (string, error) {
	reducerMap, err := s.ReduceJobData(jobID)
	if err != nil {
		return "", err
	}

	var sb strings.Builder
	writer := csv.NewWriter(&sb)

	// In the edge case of an empty map, safely return an empty string
	if len(reducerMap) == 0 {
		return "", nil
	}

	// 1. Sort the keys to ensure deterministic column ordering
	keys := make([]string, 0, len(reducerMap))
	for k := range reducerMap {
		keys = append(keys, k)
	}
	sort.Strings(keys)

	// 2. Write CSV Header
	if err := writer.Write(keys); err != nil {
		return "", fmt.Errorf("csv header write failed: %v", err)
	}

	// 3. Write CSV Row Values
	row := make([]string, len(keys))
	for i, k := range keys {
		val := reducerMap[k]

		// CSV COLLISION TRAP AVOIDANCE:
		// If the value is a complex type (slice/map/table), it will panic or print memory
		// addresses natively. We must json.Marshal it into a safe string representation.
		switch v := val.(type) {
		case string:
			row[i] = v
		case nil:
			row[i] = ""
		case float64, int, bool:
			row[i] = fmt.Sprintf("%v", v)
		default:
			// Complex type (array, slice, list of dicts)
			safeJSON, jsErr := json.Marshal(v)
			if jsErr != nil {
				row[i] = fmt.Sprintf("Error encoding type %T", v)
			} else {
				row[i] = string(safeJSON)
			}
		}
	}

	if err := writer.Write(row); err != nil {
		return "", fmt.Errorf("csv data write failed: %v", err)
	}

	writer.Flush()
	return sb.String(), nil
}
