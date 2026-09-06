/*
 * OpenCode status bridge for the Praefectus Fabrum bar widget.
 *
 * OpenCode loads this file for every instance. It records the latest structured
 * session status in a per-process runtime file; the widget watcher reads those
 * files without requiring a background daemon.
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const runtimeDir =
  process.env.XDG_RUNTIME_DIR || path.join(os.homedir(), ".cache");
const statusDir = path.join(runtimeDir, "opencode-praefectus-fabrum");
const defaultProcessStartedAt = Date.now() / 1000 - process.uptime();

const SessionStatus = Object.freeze({
  IDLE: "IDLE",
  WORKING: "WORKING",
  WAITING: "WAITING",
  NEEDS_APPROVAL: "NEEDS_APPROVAL",
});

const DEFAULT_PREVIEWS = Object.freeze({
  IDLE: "idle",
  WORKING: "working",
  WAITING: "waiting for response",
  NEEDS_APPROVAL: "waiting for permission",
});

function textValue(candidateText) {
  return typeof candidateText === "string" && candidateText.trim()
    ? candidateText.trim()
    : "";
}

function permissionPreview(properties) {
  const nestedPermissionProperties = properties?.permission;
  const permissionProperties =
    nestedPermissionProperties && typeof nestedPermissionProperties === "object"
      ? { ...properties, ...nestedPermissionProperties }
      : properties || {};
  const title = textValue(permissionProperties.title);
  const requestedOperation =
    title ||
    textValue(permissionProperties.permission) ||
    textValue(permissionProperties.type);
  const patternValues = permissionProperties.patterns ?? permissionProperties.pattern;
  const patternText = Array.isArray(patternValues)
    ? patternValues.map(textValue).filter(Boolean).join(", ")
    : textValue(patternValues);

  if (!requestedOperation) return patternText || null;
  return patternText
    ? `${requestedOperation}: ${patternText}`
    : requestedOperation;
}

function questionPreview(properties) {
  const directQuestion = textValue(
    properties?.question || properties?.prompt || properties?.message,
  );
  if (directQuestion) return directQuestion;

  const questionEntries = Array.isArray(properties?.questions)
    ? properties.questions
    : [];
  for (const questionEntry of questionEntries) {
    const questionText = textValue(
      questionEntry?.question || questionEntry?.prompt || questionEntry?.message,
    );
    if (questionText) return questionText;
  }
  return null;
}

function eventPreview(event) {
  if (event?.type === "permission.asked" || event?.type === "permission.updated") {
    return permissionPreview(event.properties);
  }
  if (event?.type === "question.asked") return questionPreview(event.properties);
  return null;
}

function finiteNonNegativeNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : null;
}

function contextTokensFor(info) {
  const tokens = info?.tokens;
  if (!tokens || typeof tokens !== "object") return null;

  const total = finiteNonNegativeNumber(tokens.total);
  if (total !== null) return total;

  const tokenValues = [
    tokens.input,
    tokens.output,
    tokens.reasoning,
    tokens.cache?.read,
    tokens.cache?.write,
  ];
  if (tokenValues.every((value) => value === undefined || value === null)) {
    return null;
  }
  return tokenValues.reduce(
    (sum, value) => sum + (finiteNonNegativeNumber(value) || 0),
    0,
  );
}

function contextLimitFor(providers, providerId, modelId) {
  for (const provider of providers || []) {
    if (String(provider?.id || "") !== String(providerId || "")) continue;
    const model =
      provider?.models?.[modelId] ||
      Object.values(provider?.models || {}).find(
        (candidate) => String(candidate?.id || "") === String(modelId),
      );
    const limit = finiteNonNegativeNumber(model?.limit?.context);
    if (limit !== null && limit > 0) return limit;
  }
  return null;
}

class ContextLimitResolver {
  constructor({ client }) {
    this.client = client;
    this.providersPromise = null;
  }

  async readProviders(request) {
    try {
      return await request();
    } catch {
      return null;
    }
  }

  async providers() {
    if (!this.providersPromise) {
      this.providersPromise = Promise.all([
        typeof this.client?.config?.providers === "function"
          ? this.readProviders(() => this.client.config.providers())
          : Promise.resolve(null),
        typeof this.client?.provider?.list === "function"
          ? this.readProviders(() => this.client.provider.list())
          : Promise.resolve(null),
      ]).then(([configuredResponse, allResponse]) => {
        const configuredData = configuredResponse?.data || configuredResponse;
        const allData = allResponse?.data || allResponse;
        const configuredProviders = configuredData?.providers;
        const allProviders = allData?.all;
        return [
          ...(Array.isArray(configuredProviders) ? configuredProviders : []),
          ...(Array.isArray(allProviders) ? allProviders : []),
        ];
      });
    }
    return this.providersPromise;
  }

  async limitFor(info) {
    if (!info?.providerID || !info?.modelID) return null;
    return contextLimitFor(
      await this.providers(),
      info.providerID,
      info.modelID,
    );
  }
}

const DEFAULT_TRANSITIONS = Object.freeze({
  IDLE: Object.freeze(["WORKING", "WAITING", "NEEDS_APPROVAL"]),
  WORKING: Object.freeze(["IDLE", "NEEDS_APPROVAL", "WAITING"]),
  WAITING: Object.freeze([
    "WORKING",
    "NEEDS_APPROVAL",
    "WAITING",
    "IDLE",
  ]),
  NEEDS_APPROVAL: Object.freeze([
    "WORKING",
    "NEEDS_APPROVAL",
    "WAITING",
    "IDLE",
  ]),
});

class TransitionPolicy {
  constructor(transitions = DEFAULT_TRANSITIONS) {
    this.transitions = new Map(
      Object.entries(transitions).map(([currentState, targetStates]) => [
        currentState,
        new Set(targetStates),
      ]),
    );
  }

  canTransition(currentState, targetState) {
    return (
      currentState === targetState ||
      this.transitions.get(currentState)?.has(targetState) === true
    );
  }
}

class LifecycleStateMachine {
  constructor({
    initialState = SessionStatus.IDLE,
    policy = new TransitionPolicy(),
  } = {}) {
    this.policy = policy;
    this.currentStateValue = initialState;
  }

  get currentState() {
    return this.currentStateValue;
  }

  transitionTo(targetState) {
    if (!this.policy.canTransition(this.currentStateValue, targetState)) {
      return false;
    }
    this.currentStateValue = targetState;
    return true;
  }
}

class EventStateMapper {
  statusType(properties) {
    const status = properties?.status;
    if (typeof status === "string") return status.toLowerCase();
    return typeof status?.type === "string" ? status.type.toLowerCase() : "";
  }

  stateFor(event) {
    switch (event?.type) {
      case "session.status": {
        const status = this.statusType(event.properties || {});
        if (status === "busy" || status === "retry") {
          return SessionStatus.WORKING;
        }
        if (status === "idle") return SessionStatus.IDLE;
        return null;
      }
      case "session.idle":
        return SessionStatus.IDLE;
      case "question.asked":
        return SessionStatus.WAITING;
      case "permission.asked":
      case "permission.updated":
        return SessionStatus.NEEDS_APPROVAL;
      case "permission.replied": {
        const response = event.properties?.reply || event.properties?.response;
        return response === "reject" ? SessionStatus.IDLE : SessionStatus.WORKING;
      }
      case "question.replied":
        return SessionStatus.WORKING;
      case "question.rejected":
        return SessionStatus.IDLE;
      default:
        return null;
    }
  }
}

class StatusRecordBuilder {
  constructor({
    project,
    directory,
    processId = process.pid,
    environment = process.env,
    processStartedAt = defaultProcessStartedAt,
    clock = () => Date.now() / 1000,
  }) {
    this.project =
      project?.id ||
      project?.name ||
      (directory ? path.basename(directory) : "OpenCode") ||
      "OpenCode";
    this.directory = directory || "";
    this.processId = processId;
    this.environment = environment;
    this.processStartedAt = processStartedAt;
    this.clock = clock;
    this.sessionId = null;
    this.lastTransitionAt = processStartedAt;
    this.lastSessionState = null;
    this.preview = null;
    this.contextUsage = null;
  }

  updateSessionId(event) {
    const properties = event?.properties || {};
    const sessionId =
      properties.sessionID ||
      properties.sessionId ||
      properties.info?.sessionID ||
      properties.info?.sessionId ||
      properties.info?.id ||
      this.sessionId ||
      null;
    if (sessionId) this.sessionId = String(sessionId);
    return this.sessionId;
  }

  markTransition() {
    this.lastTransitionAt = this.clock();
  }

  updateContextUsage(info, contextLimit) {
    const contextTokens = contextTokensFor(info);
    const contextSize = finiteNonNegativeNumber(contextLimit);
    const nextContextUsage =
      contextTokens === null || contextSize === null || contextSize === 0
        ? null
        : {
            context_tokens: contextTokens,
            context_limit: contextSize,
            context_percentage: Math.round((contextTokens / contextSize) * 100),
          };
    const previousContextUsage = this.contextUsage;
    this.contextUsage = nextContextUsage;
    return JSON.stringify(previousContextUsage) !== JSON.stringify(nextContextUsage);
  }

  build(sessionState, event) {
    const currentTimestamp = this.clock();
    const requiresAttention =
      sessionState === SessionStatus.WAITING ||
      sessionState === SessionStatus.NEEDS_APPROVAL;
    const previewText = eventPreview(event);
    if (previewText) {
      this.preview = previewText;
    } else if (this.lastSessionState !== sessionState || !this.preview) {
      this.preview = DEFAULT_PREVIEWS[sessionState] || "idle";
    }
    this.lastSessionState = sessionState;

    return {
      session_id: this.updateSessionId(event) || `pid:${this.processId}`,
      project: this.project,
      state: sessionState,
      tmux_pane: this.environment.TMUX_PANE || null,
      tmux_socket: this.environment.TMUX || null,
      source_pid: this.processId,
      process_started_at: this.processStartedAt,
      directory: this.directory,
      notification_id: null,
      attention: requiresAttention,
      attention_since: requiresAttention ? this.lastTransitionAt : null,
      last_transition_ts: this.lastTransitionAt || currentTimestamp,
      preview: this.preview,
      event_type: event?.type || null,
      updated_at: currentTimestamp,
      ...(this.contextUsage || {}),
    };
  }
}

class AtomicStatusRecordWriter {
  constructor({
    directory,
    recordPath,
    fileSystem = fs,
    processId = process.pid,
    clock = () => Date.now(),
  }) {
    this.directory = directory;
    this.recordPath = recordPath;
    this.fileSystem = fileSystem;
    this.processId = processId;
    this.clock = clock;
    this.sequence = 0;
  }

  write(record) {
    let temporaryPath = null;
    try {
      this.fileSystem.mkdirSync(this.directory, {
        recursive: true,
        mode: 0o700,
      });
      this.sequence += 1;
      temporaryPath = path.join(
        this.directory,
        `.${this.processId}.${this.clock()}.${this.sequence}.tmp`,
      );
      this.fileSystem.writeFileSync(
        temporaryPath,
        `${JSON.stringify(record)}\n`,
        {
          encoding: "utf8",
          mode: 0o600,
        },
      );
      this.fileSystem.renameSync(temporaryPath, this.recordPath);
      temporaryPath = null;
    } catch {
      // Status reporting must never interfere with the OpenCode session.
    } finally {
      if (temporaryPath) {
        try {
          this.fileSystem.unlinkSync(temporaryPath);
        } catch {
          // The temporary file may already have been removed.
        }
      }
    }
  }

  remove() {
    try {
      this.fileSystem.unlinkSync(this.recordPath);
    } catch {
      // The watcher can ignore a missing record.
    }
  }
}

class OpenCodeStatusReporter {
  constructor({
    stateMachine,
    eventMapper,
    recordBuilder,
    recordWriter,
    contextLimitFor: resolveContextLimit = async () => null,
  }) {
    this.stateMachine = stateMachine;
    this.eventMapper = eventMapper;
    this.recordBuilder = recordBuilder;
    this.recordWriter = recordWriter;
    this.resolveContextLimit = resolveContextLimit;
    this.pendingRequests = new Map();
    this.requestSequence = 0;
    this.hasWrittenRecord = false;
  }

  sessionIdFor(event) {
    const properties = event?.properties || {};
    return (
      properties.sessionID ||
      properties.sessionId ||
      properties.info?.sessionID ||
      properties.info?.sessionId ||
      null
    );
  }

  requestIdFor(event) {
    const properties = event?.properties || {};
    return (
      properties.id ||
      properties.permissionID ||
      properties.permissionId ||
      properties.requestID ||
      properties.requestId ||
      event?.id ||
      `request-${++this.requestSequence}`
    );
  }

  updatePendingRequest(event) {
    const sessionId = this.sessionIdFor(event) || "__unknown__";
    const requestId = String(this.requestIdFor(event));
    const pendingRequestIds = this.pendingRequests.get(sessionId) || new Set();

    if (event?.type === "permission.asked" || event?.type === "permission.updated" || event?.type === "question.asked") {
      pendingRequestIds.add(requestId);
      this.pendingRequests.set(sessionId, pendingRequestIds);
      return;
    }

    if (event?.type !== "permission.replied" && event?.type !== "question.replied" && event?.type !== "question.rejected") {
      return;
    }

    pendingRequestIds.delete(requestId);
    if (pendingRequestIds.size === 0) this.pendingRequests.delete(sessionId);
    else this.pendingRequests.set(sessionId, pendingRequestIds);
  }

  hasPendingRequest(event) {
    const sessionId = this.sessionIdFor(event);
    if (sessionId && this.pendingRequests.get(sessionId)?.size) return true;
    if (!sessionId && this.pendingRequests.size) return true;
    return false;
  }

  async handle(event) {
    const mappedState = this.eventMapper.stateFor(event);
    const isIdleEvent =
      event?.type === "session.idle" ||
      (event?.type === "session.status" && mappedState === SessionStatus.IDLE);

    if (isIdleEvent && this.hasPendingRequest(event)) return;

    this.updatePendingRequest(event);
    if (
      (event?.type === "permission.replied" ||
        event?.type === "question.replied" ||
        event?.type === "question.rejected") &&
      this.hasPendingRequest(event)
    ) {
      return;
    }

    let contextUsageChanged = false;
    const messageInfo = event?.type === "message.updated" ? event.properties?.info : null;
    if (messageInfo?.role === "assistant" && typeof this.recordBuilder.updateContextUsage === "function") {
      let contextLimit = null;
      try {
        contextLimit = await this.resolveContextLimit(messageInfo);
      } catch {
        contextLimit = null;
      }
      contextUsageChanged = this.recordBuilder.updateContextUsage(
        messageInfo,
        contextLimit,
      );
    }

    const isPreviewEvent =
      event?.type === "permission.asked" ||
      event?.type === "permission.updated" ||
      event?.type === "question.asked";

    let stateChanged = false;
    if (mappedState && mappedState !== this.stateMachine.currentState) {
      if (!this.stateMachine.transitionTo(mappedState)) return;
      this.recordBuilder.markTransition();
      stateChanged = true;
    }

    if (
      stateChanged ||
      (mappedState && !this.hasWrittenRecord) ||
      event?.type === "session.created" ||
      event?.type === "session.updated" ||
      isPreviewEvent ||
      contextUsageChanged
    ) {
      this.recordWriter.write(
        this.recordBuilder.build(this.stateMachine.currentState, event),
      );
      this.hasWrittenRecord = true;
    }
  }

  async initialize() {
    if (this.hasWrittenRecord) return;
    this.recordWriter.write(
      this.recordBuilder.build(SessionStatus.IDLE, {
        type: "server.connected",
        properties: {},
      }),
    );
    this.hasWrittenRecord = true;
  }

  async dispose() {
    this.recordWriter.remove();
  }
}

async function server({ project, directory, client }) {
  const recordPath = path.join(statusDir, `${process.pid}.json`);
  const contextLimitResolver = new ContextLimitResolver({ client });
  const reporter = new OpenCodeStatusReporter({
    stateMachine: new LifecycleStateMachine(),
    eventMapper: new EventStateMapper(),
    recordBuilder: new StatusRecordBuilder({
      project,
      directory,
      processStartedAt: defaultProcessStartedAt,
    }),
    recordWriter: new AtomicStatusRecordWriter({
      directory: statusDir,
      recordPath,
    }),
    contextLimitFor: (info) => contextLimitResolver.limitFor(info),
  });
  await reporter.initialize();

  return {
    event: async ({ event }) => reporter.handle(event),
    dispose: async () => reporter.dispose(),
  };
}

export {
  AtomicStatusRecordWriter,
  ContextLimitResolver,
  EventStateMapper,
  LifecycleStateMachine,
  OpenCodeStatusReporter,
  SessionStatus,
  StatusRecordBuilder,
  TransitionPolicy,
  contextLimitFor,
  contextTokensFor,
};

export default {
  id: "opencode-praefectus-fabrum",
  server,
};
