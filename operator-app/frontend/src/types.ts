// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

export interface ConnectionStatus {
  state: string;
  ready: boolean;
  mqtt_connected: boolean;
  changed_at: string;
}

export interface LocationView {
  latitude: number;
  longitude: number;
  altitude: number | null;
  bearing: number | null;
  accuracy: number | null;
  source: string | null;
  recorded_at: string;
}

export interface EntityView {
  key: string;
  entity_id: string;
  integration: string;
  category: string | null;
  affiliation: string;
  status: string;
  type: string | null;
  name: string | null;
  fingerprint: string | null;
  metadata: Record<string, unknown>;
  location: LocationView | null;
  location_only: boolean;
  entity_recorded_at: string | null;
  last_observed_at: string;
  age_seconds: number;
  freshness: 'fresh' | 'stale';
  entity_age_seconds: number | null;
  entity_freshness: 'fresh' | 'stale' | null;
  location_age_seconds: number | null;
  location_freshness: 'fresh' | 'stale' | null;
}

export interface DiagnosticView {
  timestamp: string;
  level: 'info' | 'warning' | 'error';
  code: string;
  message: string;
}

export interface TaskOutcomeView {
  task_id: string | null;
  target_key: string;
  command: string;
  mode: 'complete' | 'acknowledgment';
  status: string;
  detail: string;
  completed_at: string;
}

export interface OperatorSnapshot {
  generated_at: string;
  connection: ConnectionStatus | null;
  connection_summary:
    | 'ready'
    | 'reconnecting'
    | 'retry scheduled'
    | 'credentials rejected'
    | 'credentials unavailable'
    | 'subscription denied'
    | 'subscription resource-limited'
    | 'terminal'
    | 'disconnected'
    | null;
  entities: EntityView[];
  diagnostics: DiagnosticView[];
  task_outcomes: TaskOutcomeView[];
  health: {
    entity_watcher_active: boolean;
    location_watcher_active: boolean;
    entity_scope_pairs: number;
    location_scope_filters: number;
    entity_dropped_events: number;
    location_dropped_events: number;
    entity_decode_errors: number;
    location_decode_errors: number;
    browser_clients: number;
    browser_dropped_updates: number;
  } | null;
}

export interface JSONSchemaProperty {
  type?: 'string' | 'integer' | 'number' | 'boolean';
  title?: string;
  description?: string;
  default?: string | number | boolean;
  minLength?: number;
  maxLength?: number;
  minimum?: number;
  maximum?: number;
}

export interface CommandDefinition {
  name: string;
  label: string;
  description: string;
  allowedIntegrations: string[];
  mode: 'complete' | 'acknowledgment';
  requestSchema: {
    type: 'object';
    properties?: Record<string, JSONSchemaProperty>;
    required?: string[];
    additionalProperties: false;
  };
}

export interface SafeConfiguration {
  mode: 'mock' | 'live';
  read_only: boolean;
  tasking_enabled: boolean;
  integrations: string[];
  categories: string[];
  stale_after_seconds: number;
  maximum_entities: number;
  commands: CommandDefinition[];
  basemap_url_template: string | null;
  basemap_attribution: string;
}

export interface PreparedTask {
  preparation_token: string;
  expires_at: string;
  target_key: string;
  target_label: string;
  command: string;
  mode: 'complete' | 'acknowledgment';
  payload: Record<string, unknown>;
  warning: string;
}

export interface TaskConfirmation {
  task_id: string | null;
  target_key: string;
  command: string;
  mode: 'complete' | 'acknowledgment';
  status: string;
  detail: string;
  completed_at: string;
}
