{{/* Chart name / fullname / labels */}}
{{- define "kubehuddle.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "kubehuddle.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := include "kubehuddle.name" . -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "kubehuddle.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "kubehuddle.labels" -}}
helm.sh/chart: {{ include "kubehuddle.chart" . }}
{{ include "kubehuddle.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "kubehuddle.selectorLabels" -}}
app.kubernetes.io/name: {{ include "kubehuddle.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "kubehuddle.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "kubehuddle.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* Component names */}}
{{- define "kubehuddle.engine.fullname" -}}{{ include "kubehuddle.fullname" . }}-engine{{- end -}}
{{- define "kubehuddle.ui.fullname" -}}{{ include "kubehuddle.fullname" . }}-ui{{- end -}}
{{- define "kubehuddle.postgres.fullname" -}}{{ include "kubehuddle.fullname" . }}-postgres{{- end -}}

{{/* Database secret + DSN */}}
{{- define "kubehuddle.dbSecretName" -}}
{{- if .Values.database.existingSecret -}}
{{- .Values.database.existingSecret -}}
{{- else -}}
{{- include "kubehuddle.fullname" . }}-db
{{- end -}}
{{- end -}}

{{- define "kubehuddle.dbSecretKey" -}}
{{- .Values.database.existingSecretKey | default "dsn" -}}
{{- end -}}

{{- define "kubehuddle.dbDsn" -}}
{{- if .Values.postgres.enabled -}}
postgres://{{ .Values.postgres.auth.username }}:{{ .Values.postgres.auth.password }}@{{ include "kubehuddle.postgres.fullname" . }}:5432/{{ .Values.postgres.auth.database }}?sslmode=disable
{{- else -}}
postgres://{{ .Values.database.user }}:{{ .Values.database.password }}@{{ .Values.database.host }}:{{ .Values.database.port }}/{{ .Values.database.name }}?sslmode={{ .Values.database.sslmode }}
{{- end -}}
{{- end -}}

{{/* Reusable env: DB driver + DSN (from the secret) */}}
{{- define "kubehuddle.dbEnv" -}}
- name: KUBEHUDDLE_DB_DRIVER
  value: postgres
- name: KUBEHUDDLE_DB_DSN
  valueFrom:
    secretKeyRef:
      name: {{ include "kubehuddle.dbSecretName" . }}
      key: {{ include "kubehuddle.dbSecretKey" . }}
{{- end -}}

{{/* Hardened container security context */}}
{{- define "kubehuddle.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end -}}
