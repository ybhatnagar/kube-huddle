package kubernetes

import (
	"fmt"
	"strconv"
	"strings"
)

// parseCPUMilli parses k8s CPU quantities into millicores.
// Accepts: "100m", "0.1", "1", "2500m".
func parseCPUMilli(s string) (int64, error) {
	s = strings.TrimSpace(s)
	if s == "" {
		return 0, fmt.Errorf("empty cpu quantity")
	}
	if strings.HasSuffix(s, "m") {
		n, err := strconv.ParseInt(strings.TrimSuffix(s, "m"), 10, 64)
		if err != nil {
			return 0, err
		}
		return n, nil
	}
	f, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return 0, err
	}
	return int64(f * 1000), nil
}

// parseMemBytes parses k8s memory quantities into bytes.
// Accepts binary IEC suffixes ("Ki","Mi","Gi","Ti") and decimal SI ("K","M","G","T").
func parseMemBytes(s string) (int64, error) {
	s = strings.TrimSpace(s)
	if s == "" {
		return 0, fmt.Errorf("empty memory quantity")
	}
	multipliers := []struct {
		suffix string
		mult   int64
	}{
		{"Ki", 1 << 10}, {"Mi", 1 << 20}, {"Gi", 1 << 30}, {"Ti", 1 << 40},
		{"K", 1_000}, {"M", 1_000_000}, {"G", 1_000_000_000}, {"T", 1_000_000_000_000},
	}
	for _, m := range multipliers {
		if strings.HasSuffix(s, m.suffix) {
			num := strings.TrimSuffix(s, m.suffix)
			n, err := strconv.ParseInt(num, 10, 64)
			if err != nil {
				return 0, err
			}
			return n * m.mult, nil
		}
	}
	return strconv.ParseInt(s, 10, 64)
}
