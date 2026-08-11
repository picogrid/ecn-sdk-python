// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

import L, { type Marker, type TileLayer } from 'leaflet';
import 'leaflet/dist/leaflet.css';
import './styles.css';

import {
  HTTPResponseError,
  confirmTask,
  discardTaskPreparation,
  loadConfiguration,
  loadState,
  prepareTask,
  retireBrowserView,
  stateWebSocket,
} from './api';
import type {
  CommandDefinition,
  EntityView,
  JSONSchemaProperty,
  OperatorSnapshot,
  PreparedTask,
  SafeConfiguration,
} from './types';

function element<T extends HTMLElement>(id: string): T {
  const found = document.getElementById(id);
  if (!found) throw new Error(`missing required element ${id}`);
  return found as T;
}

type Theme = 'light' | 'dark';

const themeStorageKey = 'picogrid-ecn-operator-theme';
const viewIdentityStorageKey = 'picogrid-ecn-operator-view-id';
const duplicateViewCloseCode = 1013;
const duplicateViewCloseReason = 'operator view identity is already in use';
const duplicateViewRetryLimit = 3;
const duplicateViewRetryDelayMilliseconds = 25;
const stateSocketAcceptanceTimeoutMilliseconds = 5_000;
const definiteNoPublicationResponseStatuses: Record<number, true> = {
  400: true,
  403: true,
  409: true,
  413: true,
  422: true,
};
const confirmationErrorOutcomeStatuses: Record<number, readonly string[]> = {
  409: ['RECONNECT', 'OUTCOME_UNKNOWN'],
  502: ['FAILED'],
  503: ['RECONNECT'],
  504: ['TIMEOUT'],
};
const canonicalUuidPattern =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

interface InitialViewIdentity {
  id: string;
  persistent: boolean;
}

function initialViewIdentity(): InitialViewIdentity {
  const generated = window.crypto.randomUUID();
  try {
    const stored = window.sessionStorage.getItem(viewIdentityStorageKey);
    if (stored === null) {
      window.sessionStorage.setItem(viewIdentityStorageKey, generated);
      return { id: generated, persistent: true };
    }
    if (!canonicalUuidPattern.test(stored)) {
      window.sessionStorage.setItem(viewIdentityStorageKey, generated);
      return { id: generated, persistent: true };
    }
    return { id: stored.toLowerCase(), persistent: true };
  } catch {
    return { id: generated, persistent: false };
  }
}

function initialTheme(): Theme {
  try {
    const stored = window.localStorage.getItem(themeStorageKey);
    if (stored === 'light' || stored === 'dark') return stored;
  } catch {
    // A disabled storage API must not block read-only observation.
  }
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

let theme: Theme = initialTheme();
document.documentElement.dataset.theme = theme;

const map = L.map('map', {
  attributionControl: false,
  minZoom: 2,
  maxZoom: 16,
  worldCopyJump: false,
}).setView([34.05, -118.24], 10);

const graticule = L.layerGroup().addTo(map);

function colorToken(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function renderGraticule(): void {
  graticule.clearLayers();
  L.rectangle(
    [
      [-85, -180],
      [85, 180],
    ],
    {
      color: colorToken('--grid-major'),
      fillColor: colorToken('--map-background'),
      fillOpacity: 1,
      weight: 1,
      interactive: false,
    },
  ).addTo(graticule);
  for (let latitude = -80; latitude <= 80; latitude += 10) {
    L.polyline(
      [
        [latitude, -180],
        [latitude, 180],
      ],
      {
        color: latitude === 0 ? colorToken('--grid-major') : colorToken('--grid-line'),
        weight: latitude === 0 ? 1.2 : 0.6,
        interactive: false,
      },
    ).addTo(graticule);
  }
  for (let longitude = -180; longitude <= 180; longitude += 10) {
    L.polyline(
      [
        [-85, longitude],
        [85, longitude],
      ],
      {
        color: longitude === 0 ? colorToken('--grid-major') : colorToken('--grid-line'),
        weight: longitude === 0 ? 1.2 : 0.6,
        interactive: false,
      },
    ).addTo(graticule);
  }
}

renderGraticule();

const initialBrowserView = initialViewIdentity();
let configuration: SafeConfiguration | null = null;
let snapshot: OperatorSnapshot | null = null;
let selectedKey: string | null = null;
type PreparationStatus = 'review' | 'invalidating' | 'stranded';

let review: {
  task: PreparedTask;
  viewId: string;
  viewGeneration: string;
  status: PreparationStatus;
} | null = null;
let deferredStrandedPreparation: {
  task: PreparedTask;
  viewId: string;
  viewGeneration: string;
  errorMessage?: string;
} | null = null;
let socket: WebSocket | null = null;
let recoverySocket: WebSocket | null = null;
let activeViewId = initialBrowserView.id;
let activeViewGeneration = window.crypto.randomUUID();
let viewIdentityPersistent = initialBrowserView.persistent;
let viewGeneration = 0;
let preparationGeneration = 0;
let pendingPreparationCount = 0;
let pendingPreparationWaiters: Array<() => void> = [];
let connectionTransition: Promise<void> | null = null;
let pageActive = true;
let browserConnection:
  | 'connecting'
  | 'connected'
  | 'disconnected'
  | 'duplicate'
  | 'offline'
  | 'reconnect' = 'connecting';
let firstFit = true;
let basemap: TileLayer | null = null;
let basemapTileErrors = 0;
let renderedTaskCommandName: string | null = null;
const markers = new Map<string, Marker>();
const categoryFilters = new Set<string>();
const affiliationFilters = new Set<string>();

const connectionPill = element<HTMLSpanElement>('connection-pill');
const modeLabel = element<HTMLParagraphElement>('mode-label');
const entityList = element<HTMLDivElement>('entity-list');
const diagnosticList = element<HTMLOListElement>('diagnostic-list');
const categoryFilterRoot = element<HTMLDivElement>('category-filters');
const affiliationFilterRoot = element<HTMLDivElement>('affiliation-filters');
const armTasking = element<HTMLInputElement>('arm-tasking');
const taskCommand = element<HTMLSelectElement>('task-command');
const taskFields = element<HTMLDivElement>('task-fields');
const prepareButton = element<HTMLButtonElement>('prepare-task');
const taskPolicy = element<HTMLParagraphElement>('task-policy');
const taskOutcome = element<HTMLOutputElement>('task-outcome');
const taskOutcomes = element<HTMLOListElement>('task-outcomes');
const confirmDialog = element<HTMLDialogElement>('confirm-dialog');
const confirmCheck = element<HTMLInputElement>('confirm-check');
const confirmButton = element<HTMLButtonElement>('confirm-task');
const confirmInvalidation = element<HTMLParagraphElement>('confirm-invalidation');
const cancelConfirmButton = element<HTMLButtonElement>('cancel-confirm');
const recoverViewButton = element<HTMLButtonElement>('recover-view');
const reconnectViewButton = element<HTMLButtonElement>('reconnect-view');
const themeToggle = element<HTMLButtonElement>('theme-toggle');
const mapPanelElement = document.querySelector<HTMLElement>('.map-panel');
if (!mapPanelElement) throw new Error('missing required map panel');
const mapPanel: HTMLElement = mapPanelElement;
const basemapStatus = element<HTMLSpanElement>('basemap-status');
const basemapAttribution = element<HTMLSpanElement>('basemap-attribution');
const mapCoordinate = element<HTMLSpanElement>('map-coordinate');
const categoryLegendItems = element<HTMLDivElement>('category-legend-items');
const affiliationLegendItems = element<HTMLDivElement>('affiliation-legend-items');

const affiliationColors: Record<string, string> = {
  FRIEND: '#17c063',
  HOSTILE: '#f44336',
  SUSPECT: '#ffb74d',
  NEUTRAL: '#5281e6',
  UNKNOWN: '#727272',
};

const affiliationLabels: Record<string, string> = {
  FRIEND: 'Friend',
  HOSTILE: 'Hostile',
  SUSPECT: 'Suspect',
  NEUTRAL: 'Neutral',
  UNKNOWN: 'Unknown',
};

const affiliationCodes: Record<string, string> = {
  FRIEND: 'FR',
  HOSTILE: 'HO',
  SUSPECT: 'SU',
  NEUTRAL: 'NE',
  UNKNOWN: 'UN',
};

const affiliations = ['FRIEND', 'HOSTILE', 'SUSPECT', 'NEUTRAL', 'UNKNOWN'] as const;

const categoryGlyphs: Record<string, string> = {
  TRACK: 'T',
  DETECTION: 'D',
  DEVICE: 'V',
  SYSTEM: 'S',
  SENSOR: 'N',
  ALERT: 'A',
  GEOMETRIC: 'G',
  LOCATION_ONLY: 'L',
  UNKNOWN: '?',
};

const categoryLabels: Record<string, string> = {
  TRACK: 'Track',
  DETECTION: 'Detection',
  DEVICE: 'Device',
  SYSTEM: 'System',
  SENSOR: 'Sensor',
  ALERT: 'Alert',
  GEOMETRIC: 'Geometric',
  LOCATION_ONLY: 'Location / PLI',
  UNKNOWN: 'Unknown',
};

function applyTheme(nextTheme: Theme, options: { remember: boolean }): void {
  theme = nextTheme;
  document.documentElement.dataset.theme = nextTheme;
  themeToggle.setAttribute('aria-label', `Use ${nextTheme === 'dark' ? 'light' : 'dark'} theme`);
  themeToggle.title = `Use ${nextTheme === 'dark' ? 'light' : 'dark'} theme`;
  if (options.remember) {
    try {
      window.localStorage.setItem(themeStorageKey, nextTheme);
    } catch {
      // Theme persistence is optional and never affects MQTT behavior.
    }
  }
  renderGraticule();
  render();
}

themeToggle.addEventListener('click', () => {
  applyTheme(theme === 'dark' ? 'light' : 'dark', { remember: true });
});

applyTheme(theme, { remember: false });

function formatCoordinate(value: number, positive: string, negative: string): string {
  return `${Math.abs(value).toFixed(4)}° ${value >= 0 ? positive : negative}`;
}

function updateCoordinate(latitude: number, longitude: number): void {
  mapCoordinate.textContent = `${formatCoordinate(latitude, 'N', 'S')} · ${formatCoordinate(longitude, 'E', 'W')}`;
}

map.on('mousemove', (event) => updateCoordinate(event.latlng.lat, event.latlng.lng));
map.on('mouseout', () => {
  const center = map.getCenter();
  updateCoordinate(center.lat, center.lng);
});
map.on('moveend', () => {
  const center = map.getCenter();
  updateCoordinate(center.lat, center.lng);
});

function configureBasemap(config: SafeConfiguration): void {
  if (basemap) {
    basemap.removeFrom(map);
    basemap = null;
  }
  basemapTileErrors = 0;
  basemapAttribution.textContent = config.basemap_attribution;
  if (!config.basemap_url_template) {
    basemapStatus.textContent = 'Offline graticule';
    return;
  }
  const candidate = L.tileLayer(config.basemap_url_template, {
    minZoom: 2,
    maxZoom: 16,
    attribution: '',
    crossOrigin: true,
  });
  candidate.on('loading', () => {
    basemapStatus.textContent = 'Loading configured basemap';
  });
  candidate.on('load', () => {
    basemapStatus.textContent = 'Configured basemap';
  });
  candidate.on('tileerror', () => {
    basemapTileErrors += 1;
    if (basemapTileErrors < 3 || basemap !== candidate) return;
    candidate.removeFrom(map);
    basemap = null;
    basemapStatus.textContent = 'Basemap unavailable · offline graticule';
    basemapAttribution.textContent = 'Offline graticule · WGS 84';
  });
  candidate.addTo(map);
  basemap = candidate;
  basemapStatus.textContent = 'Loading configured basemap';
}

function currentAge(entity: EntityView): number {
  if (!snapshot) return entity.age_seconds;
  const elapsed = Math.max(0, (Date.now() - Date.parse(snapshot.generated_at)) / 1000);
  return entity.age_seconds + elapsed;
}

function elapsedSinceSnapshot(): number {
  return snapshot ? Math.max(0, (Date.now() - Date.parse(snapshot.generated_at)) / 1000) : 0;
}

function currentEntityAge(entity: EntityView): number | null {
  return entity.entity_age_seconds === null
    ? null
    : entity.entity_age_seconds + elapsedSinceSnapshot();
}

function currentLocationAge(entity: EntityView): number | null {
  return entity.location_age_seconds === null
    ? null
    : entity.location_age_seconds + elapsedSinceSnapshot();
}

function entityIsStale(entity: EntityView): boolean {
  const age = currentEntityAge(entity);
  return age === null || !configuration || age > configuration.stale_after_seconds;
}

function isStale(entity: EntityView): boolean {
  const age = entity.location ? currentLocationAge(entity) : currentEntityAge(entity);
  return age === null || !configuration || age > configuration.stale_after_seconds;
}

function runtimeIsReady(): boolean {
  return Boolean(
    browserConnection === 'connected' &&
      snapshot?.connection?.ready &&
      snapshot.connection.mqtt_connected &&
      snapshot.health?.entity_watcher_active &&
      snapshot.health.location_watcher_active,
  );
}

function preparedTaskIsEligible(): boolean {
  const selected = selectedEntity();
  return Boolean(
    viewIdentityPersistent &&
    review?.status === 'review' &&
      review.viewId === activeViewId &&
      review.viewGeneration === activeViewGeneration &&
      runtimeIsReady() &&
      selected &&
      !selected.location_only &&
      !entityIsStale(selected) &&
      selected.key === review.task.target_key &&
      Date.parse(review.task.expires_at) > Date.now(),
  );
}

function setReviewState(status: PreparationStatus): void {
  if (review) review.status = status;
  confirmDialog.dataset.state = status;
  const reviewIsActive = status === 'review';
  reconnectViewButton.disabled = status !== 'review';
  cancelConfirmButton.disabled = !reviewIsActive;
  confirmButton.disabled = !reviewIsActive || !confirmCheck.checked || !preparedTaskIsEligible();
  recoverViewButton.disabled = status !== 'stranded';
  confirmInvalidation.textContent =
    status === 'invalidating'
      ? 'Task confirmation or prepared-task invalidation is still in progress…'
      : status === 'stranded'
        ? 'The prepared task could not be invalidated. Reconnect the operator view before preparing another task.'
        : '';
}

function currentPreparationStatus(): PreparationStatus | undefined {
  return review?.status;
}

function retainPreparation(
  task: PreparedTask,
  viewId: string,
  viewConnectionGeneration: string,
  status: PreparationStatus,
): void {
  review = { task, viewId, viewGeneration: viewConnectionGeneration, status };
  element<HTMLDivElement>('confirm-summary').textContent =
    `${task.target_label} · ${task.command}`;
  element<HTMLPreElement>('confirm-payload').textContent = JSON.stringify(
    task.payload,
    null,
    2,
  );
  element<HTMLParagraphElement>('confirm-warning').textContent = task.warning;
  confirmCheck.checked = false;
  setReviewState(status);
  if (!confirmDialog.open) confirmDialog.showModal();
}

function strandedOutcomeMessage(errorMessage?: string): string {
  const prefix = errorMessage ? `${errorMessage} ` : '';
  return (
    `${prefix}The task delivery outcome is unknown. Do not retry automatically; ` +
    'reconnect the operator view and reconcile the task outcome first.'
  );
}


function beginPendingPreparation(): void {
  pendingPreparationCount += 1;
}

function finishPendingPreparation(): void {
  pendingPreparationCount -= 1;
  if (pendingPreparationCount !== 0) return;
  const waiters = pendingPreparationWaiters;
  pendingPreparationWaiters = [];
  for (const settle of waiters) settle();
}

async function waitForPendingPreparations(): Promise<void> {
  while (pendingPreparationCount > 0) {
    await new Promise<void>((resolve) => pendingPreparationWaiters.push(resolve));
  }
}

function retireTaskingView(): void {
  armTasking.checked = false;
  viewGeneration += 1;
  const previous = socket;
  socket = null;
  previous?.close();
  browserConnection = 'reconnect';
}

function deferStrandedPreparation(
  task: PreparedTask,
  viewId: string,
  viewConnectionGeneration: string,
  errorMessage?: string,
): void {
  // An in-flight confirmation or an already-stranded result has the stronger
  // delivery uncertainty. Preserve it until it settles or the operator explicitly
  // acknowledges it, then surface this prepare/discard uncertainty next.
  deferredStrandedPreparation ??= {
    task,
    viewId,
    viewGeneration: viewConnectionGeneration,
    errorMessage,
  };
  retireTaskingView();
  if (review?.status === 'invalidating') preserveInFlightPreparation();
  render();
}

function activateDeferredStrandedPreparation(): boolean {
  const deferred = deferredStrandedPreparation;
  if (!deferred) return false;
  deferredStrandedPreparation = null;
  retainPreparation(
    deferred.task,
    deferred.viewId,
    deferred.viewGeneration,
    'stranded',
  );
  taskOutcome.textContent = strandedOutcomeMessage(deferred.errorMessage);
  armTasking.checked = false;
  browserConnection = 'reconnect';
  render();
  return true;
}

function preserveInFlightPreparation(): void {
  if (review?.status !== 'invalidating') return;
  if (!confirmDialog.open) confirmDialog.showModal();
  confirmCheck.checked = false;
  confirmButton.disabled = true;
  armTasking.checked = false;
  taskOutcome.textContent =
    'The operator view changed while task confirmation or invalidation was in progress. ' +
    'The task delivery outcome is unknown while the request is pending. Do not retry or ' +
    'reconnect until it settles.';
}

function strandPreparation(errorMessage?: string): void {
  if (review) {
    setReviewState('stranded');
    if (!confirmDialog.open) confirmDialog.showModal();
  }
  taskOutcome.textContent = strandedOutcomeMessage(errorMessage);
  retireTaskingView();
  render();
}

async function dismissPreparedTask(
  message: string | null,
  options: { disarm: boolean },
): Promise<boolean> {
  if (!review) {
    // Nothing is under review, so there is no token to invalidate. Stay silent
    // rather than overwriting the last task outcome: render() calls this every
    // second while the runtime is unready.
    if (confirmDialog.open) {
      confirmDialog.close();
      if (message) taskOutcome.textContent = message;
    }
    return true;
  }
  if (review.status !== 'review') return false;

  const { task, viewId, viewGeneration: reviewViewGeneration } = review;
  setReviewState('invalidating');
  confirmCheck.checked = false;
  confirmButton.disabled = true;
  if (options.disarm) armTasking.checked = false;
  try {
    await discardTaskPreparation(
      task.preparation_token,
      viewId,
      reviewViewGeneration,
    );
    if (review?.task.preparation_token !== task.preparation_token) return true;
    review = null;
    if (activateDeferredStrandedPreparation()) return true;
    setReviewState('review');
    if (confirmDialog.open) confirmDialog.close();
    taskOutcome.textContent =
      message ?? 'Preparation discarded; nothing was published.';
    return true;
  } catch (error) {
    strandPreparation(
      error instanceof Error ? error.message : 'Prepared-task invalidation failed.',
    );
    return false;
  }
}

function abandonPreparationWithView(
  message: string | null,
  options: { recoverStranded?: boolean } = {},
): void {
  if (review?.status === 'invalidating') {
    preserveInFlightPreparation();
    return;
  }
  if (review?.status === 'stranded' && !options.recoverStranded) return;
  const abandoned = review !== null || confirmDialog.open;
  review = null;
  setReviewState('review');
  confirmCheck.checked = false;
  confirmButton.disabled = true;
  if (confirmDialog.open) confirmDialog.close();
  armTasking.checked = false;
  // Every reconnect runs this path, so stay silent unless something was really
  // abandoned; otherwise the last task outcome is replaced by a false report.
  if (abandoned && message) taskOutcome.textContent = message;
}

function visible(entity: EntityView): boolean {
  const category = entity.location_only ? 'LOCATION_ONLY' : (entity.category ?? 'UNKNOWN');
  return categoryFilters.has(category) && affiliationFilters.has(entity.affiliation);
}

function setConnection(label: string, connected: boolean): void {
  connectionPill.textContent = label;
  connectionPill.classList.toggle('connected', connected);
  connectionPill.classList.toggle('disconnected', !connected);
}

function checkboxFilter(root: HTMLElement, value: string, target: Set<string>): void {
  target.add(value);
  const label = document.createElement('label');
  label.className = 'filter-option';
  const input = document.createElement('input');
  input.type = 'checkbox';
  input.checked = true;
  input.value = value;
  input.addEventListener('change', () => {
    if (input.checked) target.add(value);
    else target.delete(value);
    render();
  });
  label.append(input, document.createTextNode(value.replace('_', ' ')));
  root.append(label);
}

function configureFilters(config: SafeConfiguration): void {
  categoryFilterRoot.replaceChildren();
  affiliationFilterRoot.replaceChildren();
  categoryFilters.clear();
  affiliationFilters.clear();
  for (const category of [...config.categories, 'LOCATION_ONLY']) {
    checkboxFilter(categoryFilterRoot, category, categoryFilters);
  }
  for (const affiliation of affiliations) {
    checkboxFilter(affiliationFilterRoot, affiliation, affiliationFilters);
  }
  configureLegends(config);
}

function configureLegends(config: SafeConfiguration): void {
  categoryLegendItems.replaceChildren();
  affiliationLegendItems.replaceChildren();
  for (const category of [...config.categories, 'LOCATION_ONLY']) {
    const knownCategory = Object.hasOwn(categoryGlyphs, category) ? category : 'UNKNOWN';
    const item = document.createElement('span');
    item.className = 'legend-item';
    item.dataset.legendCategory = knownCategory;
    const glyph = document.createElement('i');
    glyph.className = 'category-symbol';
    glyph.ariaHidden = 'true';
    glyph.textContent = categoryGlyphs[knownCategory] ?? '?';
    item.append(glyph, document.createTextNode(categoryLabels[knownCategory] ?? knownCategory));
    categoryLegendItems.append(item);
  }
  for (const affiliation of affiliations) {
    const item = document.createElement('span');
    item.className = 'legend-item';
    item.dataset.legendAffiliation = affiliation;
    const dot = document.createElement('i');
    dot.className = `affiliation-dot ${affiliation.toLowerCase()}`;
    dot.ariaHidden = 'true';
    item.append(dot, document.createTextNode(affiliationLabels[affiliation] ?? affiliation));
    affiliationLegendItems.append(item);
  }
}

function renderMarkers(entities: EntityView[]): void {
  const active = new Set<string>();
  const bounds: L.LatLngExpression[] = [];
  for (const entity of entities) {
    if (!visible(entity) || !entity.location) continue;
    active.add(entity.key);
    const position: L.LatLngExpression = [entity.location.latitude, entity.location.longitude];
    bounds.push(position);
    const stale = isStale(entity);
    const selected = selectedKey === entity.key;
    const category = entity.location_only ? 'LOCATION_ONLY' : (entity.category ?? 'UNKNOWN');
    const knownCategory = Object.hasOwn(categoryGlyphs, category) ? category : 'UNKNOWN';
    const categoryClass = knownCategory.toLowerCase().replaceAll('_', '-');
    const markerColor = affiliationColors[entity.affiliation] ?? affiliationColors.UNKNOWN;
    const freshness = stale ? 'STALE' : 'FRESH';
    const categoryLabel = categoryLabels[knownCategory] ?? knownCategory;
    const affiliationLabel = affiliationLabels[entity.affiliation] ?? 'Unknown';
    const affiliationCode = affiliationCodes[entity.affiliation] ?? affiliationCodes.UNKNOWN;
    const entityLabel = entity.name ?? entity.type ?? entity.entity_id;
    const markerLabel = `${entityLabel}; category ${categoryLabel}; affiliation ${affiliationLabel}; freshness ${freshness}`;
    const icon = L.divIcon({
      className: `entity-marker category-${categoryClass} ${stale ? 'stale' : 'fresh'} ${selected ? 'selected' : ''}`,
      html: `<i class="marker-glyph" style="--marker-color:${markerColor}" aria-hidden="true"><span class="marker-category-label">${categoryGlyphs[knownCategory]}</span><span class="marker-affiliation-label">${affiliationCode}</span><span class="marker-freshness-label">${stale ? 'S' : 'F'}</span></i>`,
      iconAnchor: [14, 14],
      iconSize: [28, 28],
    });
    let marker = markers.get(entity.key);
    if (!marker) {
      marker = L.marker(position, {
        icon,
        keyboard: true,
        riseOnHover: true,
        title: markerLabel,
      }).addTo(map);
      marker.on('click', () => {
        void selectEntity(entity.key);
      });
      markers.set(entity.key, marker);
    } else {
      marker.setLatLng(position);
      marker.setIcon(icon);
    }
    const markerElement = marker.getElement();
    markerElement?.setAttribute('aria-label', markerLabel);
    markerElement?.setAttribute('title', markerLabel);
    markerElement?.setAttribute('data-affiliation', entity.affiliation);
    markerElement?.setAttribute('data-freshness', freshness);
    const tooltip = document.createElement('span');
    tooltip.textContent = markerLabel;
    if (marker.getTooltip()) marker.setTooltipContent(tooltip);
    else marker.bindTooltip(tooltip, { direction: 'top' });
  }
  for (const [key, marker] of markers) {
    if (!active.has(key)) {
      marker.removeFrom(map);
      markers.delete(key);
    }
  }
  if (firstFit && bounds.length) {
    map.fitBounds(L.latLngBounds(bounds), { maxZoom: 11, padding: [36, 36] });
    firstFit = false;
  }
}

async function selectEntity(key: string): Promise<void> {
  if (key !== selectedKey) {
    preparationGeneration += 1;
    if (review) {
      await dismissPreparedTask(
        'Preparation discarded after the selected target changed; nothing was published.',
        { disarm: true },
      );
    }
  }
  selectedKey = key;
  render();
}

function selectedEntity(): EntityView | null {
  return snapshot?.entities.find((entity) => entity.key === selectedKey) ?? null;
}

function renderSelection(): void {
  const selected = selectedEntity();
  element<HTMLDivElement>('empty-selection').hidden = Boolean(selected);
  const panel = element<HTMLDivElement>('selection');
  panel.hidden = !selected;
  if (!selected) {
    renderTaskControls();
    return;
  }
  element<HTMLHeadingElement>('selection-name').textContent =
    selected.name ?? selected.type ?? selected.entity_id;
  const fields = element<HTMLDListElement>('selection-fields');
  fields.replaceChildren();
  const values: Array<[string, string]> = [
    ['UUID', selected.entity_id],
    ['Integration', selected.integration],
    ['Category', selected.location_only ? 'LOCATION ONLY' : (selected.category ?? 'UNKNOWN')],
    ['Affiliation', selected.affiliation],
    ['Status', selected.status],
    [
      'Entity state',
      selected.entity_age_seconds === null
        ? 'NOT OBSERVED'
        : `${entityIsStale(selected) ? 'STALE' : 'FRESH'} · ${currentEntityAge(selected)?.toFixed(1)} s`,
    ],
    [
      'Location',
      selected.location_age_seconds === null
        ? 'NOT OBSERVED'
        : `${isStale(selected) ? 'STALE' : 'FRESH'} · ${currentLocationAge(selected)?.toFixed(1)} s`,
    ],
  ];
  if (selected.location) {
    values.push([
      'Position',
      `${selected.location.latitude.toFixed(5)}, ${selected.location.longitude.toFixed(5)}`,
    ]);
  }
  for (const [term, value] of values) {
    const dt = document.createElement('dt');
    dt.textContent = term;
    const dd = document.createElement('dd');
    dd.textContent = value;
    fields.append(dt, dd);
  }
  element<HTMLPreElement>('selection-metadata').textContent = JSON.stringify(
    selected.metadata,
    null,
    2,
  );
  renderTaskControls();
}

function renderEntityList(entities: EntityView[]): void {
  const activeElement = document.activeElement;
  const focusedKey =
    activeElement instanceof HTMLButtonElement && activeElement.parentElement === entityList
      ? (activeElement.dataset.entityKey ?? null)
      : null;
  let focusReplacement: HTMLButtonElement | null = null;
  entityList.replaceChildren();
  for (const entity of entities.filter(visible)) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `entity-row ${selectedKey === entity.key ? 'selected' : ''}`;
    button.dataset.entityKey = entity.key;
    button.addEventListener('click', () => {
      void selectEntity(entity.key);
    });
    const title = document.createElement('strong');
    title.textContent = entity.name ?? entity.type ?? entity.entity_id;
    const detail = document.createElement('span');
    const freshness = isStale(entity) ? 'STALE' : 'FRESH';
    detail.textContent = `${entity.integration} · ${entity.location_only ? 'LOCATION ONLY' : entity.category} · ${entity.affiliation} · ${freshness} · ${currentAge(entity).toFixed(1)}s`;
    const state = document.createElement('i');
    state.className = isStale(entity) ? 'stale-dot' : 'fresh-dot';
    state.ariaHidden = 'true';
    button.append(state, title, detail);
    entityList.append(button);
    if (entity.key === focusedKey) focusReplacement = button;
  }
  focusReplacement?.focus({ preventScroll: true });
}

function renderDiagnostics(): void {
  diagnosticList.replaceChildren();
  const health = snapshot?.health;
  const healthItems: Array<[string, string, string]> = health
    ? [
        [
          'entity_watcher',
          health.entity_watcher_active
            ? health.entity_decode_errors || health.entity_dropped_events
              ? 'warning'
              : 'info'
            : 'error',
          `${health.entity_watcher_active ? 'active' : 'inactive'} · ${health.entity_scope_pairs} scope pairs · ${health.entity_dropped_events} dropped · ${health.entity_decode_errors} decode errors`,
        ],
        [
          'location_watcher',
          health.location_watcher_active
            ? health.location_decode_errors || health.location_dropped_events
              ? 'warning'
              : 'info'
            : 'error',
          `${health.location_watcher_active ? 'active' : 'inactive'} · ${health.location_scope_filters} fixed-depth filters · ${health.location_dropped_events} dropped · ${health.location_decode_errors} decode errors`,
        ],
        [
          'browser_fanout',
          health.browser_dropped_updates ? 'warning' : 'info',
          `${health.browser_clients} connected · ${health.browser_dropped_updates} dropped`,
        ],
      ]
    : [];
  for (const [code, level, message] of healthItems) {
    const item = document.createElement('li');
    item.className = level;
    const title = document.createElement('strong');
    title.textContent = code;
    const text = document.createElement('span');
    text.textContent = message;
    item.append(title, text);
    diagnosticList.append(item);
  }
  for (const diagnostic of snapshot?.diagnostics.slice(0, 17) ?? []) {
    const item = document.createElement('li');
    item.className = diagnostic.level;
    const title = document.createElement('strong');
    title.textContent = diagnostic.code;
    const text = document.createElement('span');
    text.textContent = diagnostic.message;
    item.append(title, text);
    diagnosticList.append(item);
  }
}

function renderTaskOutcomes(): void {
  taskOutcomes.replaceChildren();
  for (const outcome of snapshot?.task_outcomes ?? []) {
    const item = document.createElement('li');
    item.dataset.taskStatus = outcome.status;
    const title = document.createElement('strong');
    title.textContent = `${outcome.command} · ${outcome.status}`;
    const detail = document.createElement('span');
    const mode = outcome.mode === 'acknowledgment' ? 'ACK mode' : 'complete mode';
    detail.textContent = `${mode} · ${outcome.detail}`;
    item.append(title, detail);
    taskOutcomes.append(item);
  }
}

function eligibleCommands(entity: EntityView | null): CommandDefinition[] {
  if (!configuration || !entity || entity.location_only) return [];
  return configuration.commands.filter((command) =>
    command.allowedIntegrations.includes(entity.integration),
  );
}

function selectedCommand(): CommandDefinition | null {
  return configuration?.commands.find((command) => command.name === taskCommand.value) ?? null;
}

function renderSchemaFields(command: CommandDefinition | null): void {
  const commandName = command?.name ?? null;
  if (commandName === renderedTaskCommandName) return;
  renderedTaskCommandName = commandName;
  const previous = new Map<string, string | boolean>();
  for (const input of taskFields.querySelectorAll<HTMLInputElement>('[data-schema-field]')) {
    const name = input.dataset.schemaField;
    if (name) previous.set(name, input.type === 'checkbox' ? input.checked : input.value);
  }
  taskFields.replaceChildren();
  if (!command) return;
  const required = new Set(command.requestSchema.required ?? []);
  for (const [name, schema] of Object.entries(command.requestSchema.properties ?? {})) {
    const label = document.createElement('label');
    label.textContent = schema.title ?? name;
    const input = document.createElement('input');
    input.dataset.schemaField = name;
    input.dataset.schemaType = schema.type ?? 'string';
    input.required = required.has(name);
    input.type = schema.type === 'boolean' ? 'checkbox' : schema.type === 'number' || schema.type === 'integer' ? 'number' : 'text';
    if (typeof schema.minimum === 'number') input.min = String(schema.minimum);
    if (typeof schema.maximum === 'number') input.max = String(schema.maximum);
    if (typeof schema.minLength === 'number') input.minLength = schema.minLength;
    if (typeof schema.maxLength === 'number') input.maxLength = schema.maxLength;
    if (schema.default !== undefined) {
      if (input.type === 'checkbox') input.checked = Boolean(schema.default);
      else input.value = String(schema.default);
    }
    const prior = previous.get(name);
    if (typeof prior === 'boolean' && input.type === 'checkbox') input.checked = prior;
    if (typeof prior === 'string' && input.type !== 'checkbox') input.value = prior;
    if (schema.description) input.title = schema.description;
    label.append(input);
    taskFields.append(label);
  }
}

function renderTaskControls(): void {
  const selected = selectedEntity();
  const commands = eligibleCommands(selected);
  const enabledByPolicy = configuration?.tasking_enabled ?? false;
  const ready = runtimeIsReady();
  const targetIsReady = Boolean(selected && !selected.location_only && !entityIsStale(selected));
  armTasking.disabled =
    !enabledByPolicy || !viewIdentityPersistent || !ready || !targetIsReady;
  if (armTasking.disabled) armTasking.checked = false;
  taskPolicy.textContent = !enabledByPolicy
    ? 'Read-only deployment: tasking is disabled.'
    : !viewIdentityPersistent
      ? 'Tasking is disabled because this browser tab cannot preserve its operator view identity. Enable session storage and reload.'
      : !ready
      ? 'Tasking is unavailable until this view and both bounded MQTT watchers are ready. The browser session is disarmed.'
      : !targetIsReady
        ? 'Select a fresh observed entity before arming task controls.'
        : 'Deployment permits allowlisted commands. Controls remain disarmed in each browser session.';

  const prior = taskCommand.value;
  taskCommand.replaceChildren();
  const placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.textContent = commands.length ? 'Choose an allowlisted command' : 'No command for selection';
  taskCommand.append(placeholder);
  for (const command of commands) {
    const option = document.createElement('option');
    option.value = command.name;
    option.textContent = `${command.label}${command.mode === 'acknowledgment' ? ' (ACK)' : ''}`;
    taskCommand.append(option);
  }
  if (commands.some((command) => command.name === prior)) taskCommand.value = prior;
  taskCommand.disabled =
    !enabledByPolicy ||
    !viewIdentityPersistent ||
    !ready ||
    !armTasking.checked ||
    !targetIsReady;
  renderSchemaFields(selectedCommand());
  prepareButton.disabled = taskCommand.disabled || !taskCommand.value;
  confirmButton.disabled = !confirmCheck.checked || !preparedTaskIsEligible();
}

function payloadFromForm(command: CommandDefinition): Record<string, unknown> {
  const payload: Record<string, unknown> = {};
  const properties = command.requestSchema.properties ?? {};
  for (const input of taskFields.querySelectorAll<HTMLInputElement>('[data-schema-field]')) {
    const name = input.dataset.schemaField;
    if (!name) continue;
    const schema: JSONSchemaProperty | undefined = properties[name];
    if (input.type === 'checkbox') payload[name] = input.checked;
    else if (schema?.type === 'number') payload[name] = Number(input.value);
    else if (schema?.type === 'integer') payload[name] = Number.parseInt(input.value, 10);
    else payload[name] = input.value;
  }
  return payload;
}

function render(): void {
  if (!snapshot) return;
  const connected = runtimeIsReady();
  const connectionLabel = connected
    ? (snapshot.connection_summary ?? 'ready')
    : browserConnection === 'offline'
      ? 'browser offline'
      : browserConnection === 'reconnect'
        ? 'reconnect required'
        : browserConnection === 'duplicate'
          ? 'view identity conflict'
          : browserConnection === 'connecting'
            ? 'connecting'
            : (snapshot.connection_summary ?? 'view disconnected');
  setConnection(connectionLabel, connected);
  mapPanel.classList.toggle('runtime-disconnected', !connected);
  if (browserConnection === 'connected' && !runtimeIsReady()) {
    void dismissPreparedTask(
      'Preparation discarded because the browser view or MQTT watchers became unavailable; nothing was published. Reconnect and prepare again.',
      { disarm: true },
    );
  } else if (review && !preparedTaskIsEligible()) {
    void dismissPreparedTask(
      'Preparation discarded because the target became stale, changed, or the token expired; nothing was published. Prepare again.',
      { disarm: true },
    );
  }
  const entities = snapshot.entities;
  renderMarkers(entities);
  renderEntityList(entities);
  renderSelection();
  renderDiagnostics();
  renderTaskOutcomes();
  const visibleEntities = entities.filter(visible);
  element<HTMLSpanElement>('visible-count').textContent = String(visibleEntities.length);
  element<HTMLSpanElement>('stale-count').textContent = String(
    visibleEntities.filter(isStale).length,
  );
  element<HTMLSpanElement>('location-only-count').textContent = String(
    visibleEntities.filter((entity) => entity.location_only).length,
  );
}

async function connectState(retireCurrentView = false): Promise<void> {
  if (connectionTransition) return connectionTransition;
  const transition = connectStateOnce(retireCurrentView);
  connectionTransition = transition;
  try {
    await transition;
  } finally {
    if (connectionTransition === transition) connectionTransition = null;
  }
}

interface StateSocketOutcome {
  accepted: boolean;
  code: number;
  reason: string;
}

function bindStateSocket(
  candidate: WebSocket,
  options: {
    onAccepted?: () => void;
    preserveStrandedBeforeAcceptance?: boolean;
  } = {},
): Promise<StateSocketOutcome> {
  return new Promise((resolve) => {
    let accepted = false;
    let acceptanceTimer: number | undefined;
    socket = candidate;
    candidate.addEventListener('message', (event) => {
      if (socket !== candidate) return;
      if (!accepted) {
        if (acceptanceTimer !== undefined) window.clearTimeout(acceptanceTimer);
        accepted = true;
        if (recoverySocket === candidate) recoverySocket = null;
        options.onAccepted?.();
        resolve({ accepted: true, code: 1000, reason: '' });
      }
      browserConnection = 'connected';
      snapshot = JSON.parse(String(event.data)) as OperatorSnapshot;
      render();
    });
    const disconnected = (code: number, reason: string): void => {
      if (acceptanceTimer !== undefined) window.clearTimeout(acceptanceTimer);
      const ownsSocket = socket === candidate;
      const ownsRecovery = recoverySocket === candidate;
      if (!ownsSocket && !ownsRecovery) {
        resolve({ accepted: false, code, reason });
        return;
      }
      if (ownsSocket) socket = null;
      if (ownsRecovery) recoverySocket = null;
      viewGeneration += 1;
      if (!accepted && options.preserveStrandedBeforeAcceptance && review) {
        restoreStrandedRecovery();
        resolve({ accepted: false, code, reason });
        return;
      }
      browserConnection = navigator.onLine ? 'disconnected' : 'offline';
      abandonPreparationWithView(
        'Task controls are disabled locally, but backend invalidation could not be confirmed. Reconnect before preparing again.',
      );
      render();
      resolve({ accepted: false, code, reason });
    };
    acceptanceTimer = window.setTimeout(() => {
      if (accepted || (socket !== candidate && recoverySocket !== candidate)) return;
      disconnected(1006, 'state socket acceptance timed out');
      candidate.close();
    }, stateSocketAcceptanceTimeoutMilliseconds);
    candidate.addEventListener('close', (event) => {
      disconnected(event.code, event.reason);
    });
    candidate.addEventListener('error', () => {
      // The close event owns teardown so its code and reason can distinguish a
      // stale-view lease from other connection failures. WebSocket failures
      // are followed by close; this fallback covers an already-closed socket.
      window.setTimeout(() => {
        if (candidate.readyState === WebSocket.CLOSED) disconnected(1006, '');
      }, 0);
    });
  });
}

function restoreStrandedRecovery(): void {
  if (review?.status !== 'invalidating') return;
  setReviewState('stranded');
  if (!confirmDialog.open) confirmDialog.showModal();
  armTasking.checked = false;
  browserConnection = navigator.onLine ? 'reconnect' : 'offline';
  taskOutcome.textContent = strandedOutcomeMessage(
    'Backend retirement of the previous operator view was not confirmed.',
  );
  render();
}

function reportDuplicateViewConflict(retirementAcknowledged: boolean): void {
  armTasking.checked = false;
  browserConnection = 'duplicate';
  taskOutcome.textContent = retirementAcknowledged
    ? 'A successor view was refused after backend retirement was acknowledged. No further connection was attempted; reload before tasking.'
    : 'This operator view identity remains active after bounded retries. Close the other tab or reload before reconnecting; tasking remains disabled.';
  render();
}

async function connectStateOnce(retireCurrentView: boolean): Promise<void> {
  const transitionViewGeneration = viewGeneration;
  if (!pageActive) return;
  if (review?.status === 'invalidating') {
    taskOutcome.textContent =
      'Task confirmation or invalidation is still in progress. The task delivery outcome remains unknown; wait for it to finish before reconnecting and do not retry.';
    return;
  }
  if (review?.status === 'stranded') return;
  if (pendingPreparationCount > 0) {
    armTasking.checked = false;
    browserConnection = 'reconnect';
    taskOutcome.textContent =
      'Reconnect is waiting for the pending prepared-task request to settle and be retired.';
    render();
    await waitForPendingPreparations();
    if (!pageActive || viewGeneration !== transitionViewGeneration) return;
    const settledStatus = currentPreparationStatus();
    if (settledStatus === 'invalidating') {
      preserveInFlightPreparation();
      return;
    }
    if (settledStatus === 'stranded') return;
  }
  if (review) {
    const discarded = await dismissPreparedTask(
      'Preparation discarded before reconnecting the browser view; nothing was published.',
      { disarm: true },
    );
    if (!discarded) return;
    if (!pageActive || viewGeneration !== transitionViewGeneration) return;
  }
  const retiringGeneration = activeViewGeneration;
  if (retireCurrentView) {
    armTasking.checked = false;
    browserConnection = navigator.onLine ? 'connecting' : 'offline';
    render();
    try {
      await retireBrowserView(activeViewId, retiringGeneration);
    } catch {
      if (!pageActive) return;
      const previous = socket;
      socket = null;
      viewGeneration += 1;
      previous?.close();
      browserConnection = navigator.onLine ? 'reconnect' : 'offline';
      taskOutcome.textContent =
        'Backend retirement of the current operator view was not acknowledged. No successor view was opened; tasking remains disabled.';
      render();
      return;
    }
    if (!pageActive) return;
    if (
      viewGeneration !== transitionViewGeneration ||
      !navigator.onLine ||
      activeViewGeneration !== retiringGeneration
    ) {
      browserConnection = navigator.onLine ? 'reconnect' : 'offline';
      render();
      return;
    }
  }
  const previous = socket;
  socket = null;
  viewGeneration += 1;
  previous?.close();
  abandonPreparationWithView(
    'Preparation abandoned while reconnecting the browser view; nothing was published. Prepare again after readiness returns.',
  );
  browserConnection = 'connecting';
  render();
  const retryLimit = retireCurrentView ? 0 : duplicateViewRetryLimit;
  for (let duplicateRetries = 0; duplicateRetries <= retryLimit; duplicateRetries += 1) {
    activeViewGeneration = window.crypto.randomUUID();
    let outcome: StateSocketOutcome;
    try {
      outcome = await bindStateSocket(stateWebSocket(activeViewId, activeViewGeneration));
    } catch {
      browserConnection = 'disconnected';
      render();
      return;
    }
    if (outcome.accepted) return;
    const duplicateViewRefused =
      outcome.code === duplicateViewCloseCode && outcome.reason === duplicateViewCloseReason;
    if (
      !duplicateViewRefused ||
      !navigator.onLine ||
      review !== null ||
      pendingPreparationCount > 0
    ) {
      return;
    }
    if (duplicateRetries === retryLimit) {
      reportDuplicateViewConflict(retireCurrentView);
      return;
    }
    browserConnection = 'connecting';
    render();
    await new Promise<void>((resolve) => {
      window.setTimeout(resolve, duplicateViewRetryDelayMilliseconds);
    });
    if (!pageActive || !navigator.onLine || browserConnection !== 'connecting') return;
  }
}

async function recoverStrandedViewOnce(
  strandedReview: NonNullable<typeof review>,
): Promise<void> {
  const transitionViewGeneration = viewGeneration;
  if (!pageActive) return;
  const retirementViewId = strandedReview.viewId;
  if (retirementViewId !== activeViewId) {
    restoreStrandedRecovery();
    return;
  }
  const retiringGeneration = activeViewGeneration;
  try {
    await retireBrowserView(retirementViewId, retiringGeneration);
  } catch {
    if (!pageActive) return;
    restoreStrandedRecovery();
    return;
  }
  if (!pageActive) return;
  if (
    viewGeneration !== transitionViewGeneration ||
    review !== strandedReview ||
    review.status !== 'invalidating' ||
    !navigator.onLine
  ) {
    restoreStrandedRecovery();
    return;
  }
  activeViewGeneration = window.crypto.randomUUID();
  try {
    const candidate = stateWebSocket(activeViewId, activeViewGeneration);
    recoverySocket = candidate;
    const outcome = await bindStateSocket(candidate, {
      preserveStrandedBeforeAcceptance: true,
      onAccepted: () => {
        if (review === strandedReview) review = null;
        if (
          deferredStrandedPreparation?.viewId === retirementViewId &&
          deferredStrandedPreparation.viewGeneration === strandedReview.viewGeneration
        ) {
          deferredStrandedPreparation = null;
        }
        setReviewState('review');
        confirmCheck.checked = false;
        confirmButton.disabled = true;
        if (confirmDialog.open) confirmDialog.close();
        armTasking.checked = false;
        taskOutcome.textContent =
          'The previous operator view was retired. The task delivery outcome is unknown; reconcile it before attempting another task.';
      },
    });
    if (
      !outcome.accepted &&
      outcome.code === duplicateViewCloseCode &&
      outcome.reason === duplicateViewCloseReason
    ) {
      taskOutcome.textContent = strandedOutcomeMessage(
        'A successor view was refused after backend retirement was acknowledged.',
      );
      render();
    }
  } catch {
    recoverySocket = null;
    restoreStrandedRecovery();
  }
}

function recoverStrandedView(): void {
  const strandedReview = review;
  if (!strandedReview || strandedReview.status !== 'stranded' || connectionTransition) return;
  setReviewState('invalidating');
  armTasking.checked = false;
  browserConnection = 'connecting';
  taskOutcome.textContent =
    'Waiting for the backend to confirm retirement of the previous operator view. Tasking remains disabled.';
  render();

  const transition = recoverStrandedViewOnce(strandedReview);
  connectionTransition = transition;
  void transition.finally(() => {
    if (connectionTransition === transition) connectionTransition = null;
  });
}

armTasking.addEventListener('change', renderTaskControls);
taskCommand.addEventListener('change', () => {
  renderSchemaFields(selectedCommand());
  prepareButton.disabled = taskCommand.disabled || !taskCommand.value;
});

prepareButton.addEventListener('click', async () => {
  const selected = selectedEntity();
  const command = selectedCommand();
  if (!selected || !command) {
    taskOutcome.textContent = 'Select a target and allowlisted command before preparing.';
    renderTaskControls();
    return;
  }
  if (!runtimeIsReady()) {
    taskOutcome.textContent = 'The operator view is not ready; nothing was prepared or published.';
    renderTaskControls();
    return;
  }
  // Freshness can expire after the controls were last rendered. Preparing is
  // non-publishing, and the backend rechecks freshness both before issuing a
  // token and again before confirmation, so let it return an explicit denial
  // instead of silently dropping the operator's click.
  const invalid = taskFields.querySelector<HTMLInputElement>('input:invalid');
  if (invalid) {
    invalid.reportValidity();
    return;
  }
  prepareButton.disabled = true;
  taskOutcome.textContent = 'Preparing…';
  const preparedAttempt = ++preparationGeneration;
  const preparedGeneration = viewGeneration;
  const preparedForView = activeViewId;
  const preparedForViewGeneration = activeViewGeneration;
  const targetKey = selected.key;
  beginPendingPreparation();
  try {
    const candidate = await prepareTask({
      entity_id: selected.entity_id,
      integration: selected.integration,
      command: command.name,
      payload: payloadFromForm(command),
    }, preparedForView, preparedForViewGeneration);
    const currentSelected = selectedEntity();
    if (
      preparedAttempt !== preparationGeneration ||
      preparedGeneration !== viewGeneration ||
      preparedForView !== activeViewId ||
      preparedForViewGeneration !== activeViewGeneration ||
      !runtimeIsReady() ||
      currentSelected?.key !== targetKey ||
      !currentSelected ||
      entityIsStale(currentSelected)
    ) {
      try {
        await discardTaskPreparation(
          candidate.preparation_token,
          preparedForView,
          preparedForViewGeneration,
        );
        if (!review) {
          armTasking.checked = false;
          taskOutcome.textContent =
            'Preparation discarded because readiness changed; nothing was published. Reconnect and prepare again.';
        }
      } catch (error) {
        const message =
          error instanceof Error ? error.message : 'Prepared-task invalidation failed.';
        // The discard outcome is unknown. Retain the exact returned candidate before
        // stranding the view so the disabled recovery dialog cannot disappear merely
        // because this response arrived before an ordinary review was installed.
        // An in-flight or already-stranded review has stronger delivery uncertainty,
        // so keep it until it settles and defer this candidate behind it.
        if (review) {
          deferStrandedPreparation(
            candidate,
            preparedForView,
            preparedForViewGeneration,
            message,
          );
        } else {
          retainPreparation(
            candidate,
            preparedForView,
            preparedForViewGeneration,
            'stranded',
          );
          strandPreparation(message);
        }
      }
      return;
    }
    retainPreparation(
      candidate,
      preparedForView,
      preparedForViewGeneration,
      'review',
    );
    taskOutcome.textContent = 'Prepared; explicit confirmation is required.';
  } catch (error) {
    if (error instanceof HTTPResponseError) {
      const definitePreparationDenial =
        definiteNoPublicationResponseStatuses[error.status] === true ||
        (error.status === 503 && error.outcomeStatus === 'RECONNECT');
      if (definitePreparationDenial) {
        if (preparedAttempt === preparationGeneration) {
          const status = error.outcomeStatus === 'RECONNECT' ? 'RECONNECT — ' : '';
          taskOutcome.textContent = `${status}${error.message}`;
          render();
        }
        return;
      }
    }
    if (
      preparedForView === activeViewId &&
      preparedForViewGeneration === activeViewGeneration
    ) {
      retireTaskingView();
    }
    if (preparedAttempt === preparationGeneration) {
      taskOutcome.textContent =
        'The preparation result was not proven. No task was published; reconnect this operator view before preparing again.';
      render();
    }
  } finally {
    renderTaskControls();
    finishPendingPreparation();
  }
});

confirmCheck.addEventListener('change', () => {
  confirmButton.disabled = !confirmCheck.checked || !preparedTaskIsEligible();
});

confirmButton.addEventListener('click', async (event) => {
  event.preventDefault();
  if (!review || !confirmCheck.checked || !preparedTaskIsEligible()) {
    await dismissPreparedTask(
      'Preparation is no longer eligible. Reconnect if needed and prepare again; nothing was published.',
      { disarm: true },
    );
    render();
    return;
  }
  const currentReview = review;
  setReviewState('invalidating');
  confirmCheck.checked = false;
  try {
    const outcome = await confirmTask(
      currentReview.task.preparation_token,
      currentReview.viewId,
      currentReview.viewGeneration,
    );
    if (review === currentReview) {
      review = null;
      if (activateDeferredStrandedPreparation()) return;
      setReviewState('review');
    }
    taskOutcome.textContent = `${outcome.command}: ${outcome.status} — ${outcome.detail}`;
    if (review === null && confirmDialog.open) confirmDialog.close();
  } catch (error) {
    if (error instanceof HTTPResponseError) {
      const acceptedOutcome =
        error.outcomeStatus !== null &&
        confirmationErrorOutcomeStatuses[error.status]?.includes(error.outcomeStatus) === true;
      if (definiteNoPublicationResponseStatuses[error.status] === true || acceptedOutcome) {
        if (review === currentReview) {
          review = null;
          if (activateDeferredStrandedPreparation()) return;
          setReviewState('review');
        }
        const status = acceptedOutcome ? `${error.outcomeStatus} — ` : '';
        taskOutcome.textContent = `${currentReview.task.command}: ${status}${error.message}`;
        if (review === null && confirmDialog.open) confirmDialog.close();
        return;
      }
    }
    const message = error instanceof Error ? error.message : 'Task failed.';
    strandPreparation(message);
  }
});

async function discardPreparationFromBrowser(): Promise<void> {
  await dismissPreparedTask('Preparation discarded; nothing was published.', { disarm: false });
}
cancelConfirmButton.addEventListener('click', (event) => {
  event.preventDefault();
  void discardPreparationFromBrowser();
});
confirmDialog.addEventListener('cancel', (event) => {
  event.preventDefault();
  void discardPreparationFromBrowser();
});
reconnectViewButton.addEventListener('click', () => {
  void connectState(true);
});
recoverViewButton.addEventListener('click', () => {
  recoverStrandedView();
});

function closeStateSocketForBrowserTransition(): void {
  const previous = socket;
  socket = null;
  if (recoverySocket === previous) {
    recoverySocket = null;
    restoreStrandedRecovery();
  }
  previous?.close();
}

window.addEventListener('offline', () => {
  viewGeneration += 1;
  closeStateSocketForBrowserTransition();
  abandonPreparationWithView(
    'Task controls are disabled locally, but backend invalidation could not be confirmed. Reconnect before preparing again.',
  );
  browserConnection = 'offline';
  render();
});
window.addEventListener('online', () => {
  viewGeneration += 1;
  closeStateSocketForBrowserTransition();
  abandonPreparationWithView(null);
  browserConnection = 'reconnect';
  render();
});
window.addEventListener('pagehide', (event) => {
  pageActive = false;
  viewGeneration += 1;
  browserConnection = 'reconnect';
  closeStateSocketForBrowserTransition();
  abandonPreparationWithView(null);
  if (!event.persisted) {
    basemap?.removeFrom(map);
    basemap = null;
    map.remove();
  }
});
window.addEventListener('pageshow', (event) => {
  if (!event.persisted) return;
  pageActive = true;
  viewGeneration += 1;
  browserConnection = navigator.onLine ? 'reconnect' : 'offline';
  render();
  if (navigator.onLine) void connectState(true);
});

setInterval(render, 1_000);

async function start(): Promise<void> {
  try {
    [configuration, snapshot] = await Promise.all([loadConfiguration(), loadState()]);
    modeLabel.textContent = `${configuration.mode.toUpperCase()} · ${configuration.integrations.join(', ')} · max ${configuration.maximum_entities}`;
    configureBasemap(configuration);
    configureFilters(configuration);
    render();
    void connectState();
  } catch (error) {
    setConnection('startup failed', false);
    const item = document.createElement('li');
    item.className = 'error';
    const title = document.createElement('strong');
    title.textContent = 'startup';
    const detail = document.createElement('span');
    detail.textContent = error instanceof Error ? error.message : 'failed';
    item.append(title, detail);
    diagnosticList.replaceChildren(item);
  }
}

void start();
