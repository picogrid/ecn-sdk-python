// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

import type {
  OperatorSnapshot,
  PreparedTask,
  SafeConfiguration,
  TaskConfirmation,
} from './types';

const TASK_MUTATION_TIMEOUT_MS = 10_000;
// The backend task exchange has a 15-second deadline. Give it enough response
// overhead that a definite result is not converted into an unknown browser outcome.
const TASK_CONFIRM_TIMEOUT_MS = 20_000;
export class HTTPResponseError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly outcomeStatus: string | null,
  ) {
    super(message);
    this.name = 'HTTPResponseError';
  }
}


async function responseJSON<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = (await response.json().catch(() => ({ detail: 'request failed' }))) as {
      detail?: string;
      outcome_status?: string;
    };
    throw new HTTPResponseError(
      body.detail ?? `request failed (${response.status})`,
      response.status,
      body.outcome_status ?? null,
    );
  }
  return (await response.json()) as T;
}

export async function loadConfiguration(): Promise<SafeConfiguration> {
  return responseJSON<SafeConfiguration>(await fetch('/api/config', { cache: 'no-store' }));
}

export async function loadState(): Promise<OperatorSnapshot> {
  return responseJSON<OperatorSnapshot>(await fetch('/api/state', { cache: 'no-store' }));
}

export async function prepareTask(input: {
  entity_id: string;
  integration: string;
  command: string;
  payload: Record<string, unknown>;
}, viewId: string, viewGeneration: string): Promise<PreparedTask> {
  return responseJSON<PreparedTask>(
    await fetch('/api/tasks/prepare', {
      method: 'POST',
      signal: AbortSignal.timeout(TASK_MUTATION_TIMEOUT_MS),
      headers: {
        'Content-Type': 'application/json',
        'X-Operator-Intent': 'prepare',
        'X-Operator-View': viewId,
        'X-Operator-View-Generation': viewGeneration,
      },
      body: JSON.stringify(input),
    }),
  );
}

export async function confirmTask(
  preparationToken: string,
  viewId: string,
  viewGeneration: string,
): Promise<TaskConfirmation> {
  return responseJSON<TaskConfirmation>(
    await fetch('/api/tasks/confirm', {
      method: 'POST',
      signal: AbortSignal.timeout(TASK_CONFIRM_TIMEOUT_MS),
      headers: {
        'Content-Type': 'application/json',
        'X-Operator-Intent': 'confirm',
        'X-Operator-View': viewId,
        'X-Operator-View-Generation': viewGeneration,
      },
      body: JSON.stringify({ preparation_token: preparationToken, confirmed: true }),
    }),
  );
}

export async function discardTaskPreparation(
  preparationToken: string,
  viewId: string,
  viewGeneration: string,
): Promise<void> {
  const result = await responseJSON<{ discarded: boolean }>(
    await fetch('/api/tasks/discard', {
      method: 'POST',
      signal: AbortSignal.timeout(TASK_MUTATION_TIMEOUT_MS),
      headers: {
        'Content-Type': 'application/json',
        'X-Operator-Intent': 'discard',
        'X-Operator-View': viewId,
        'X-Operator-View-Generation': viewGeneration,
      },
      body: JSON.stringify({ preparation_token: preparationToken }),
    }),
  );
  if (result.discarded !== true) {
    throw new Error('prepared task was not invalidated');
  }
}

export async function retireBrowserView(
  viewId: string,
  viewGeneration: string,
): Promise<void> {
  const result = await responseJSON<{ retired: boolean }>(
    await fetch('/api/view/retire', {
      method: 'POST',
      signal: AbortSignal.timeout(TASK_MUTATION_TIMEOUT_MS),
      headers: {
        'Content-Type': 'application/json',
        'X-Operator-Intent': 'retire-view',
        'X-Operator-View': viewId,
        'X-Operator-View-Generation': viewGeneration,
      },
      body: '{}',
    }),
  );
  if (result.retired !== true) {
    throw new Error('operator view retirement was not acknowledged');
  }
}

export function stateWebSocket(viewId: string, viewGeneration: string): WebSocket {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return new WebSocket(
    `${protocol}//${window.location.host}/ws/state?view_id=${encodeURIComponent(viewId)}&view_generation=${encodeURIComponent(viewGeneration)}`,
  );
}
