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
      Object.entries(transitions).map(([state, targets]) => [
        state,
        new Set(targets),
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
    this.state = initialState;
  }

  get currentState() {
    return this.state;
  }

  transitionTo(targetState) {
    if (!this.policy.canTransition(this.state, targetState)) return false;
    this.state = targetState;
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
    processStartedAt: startedAt = defaultProcessStartedAt,
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
    this.processStartedAt = startedAt;
    this.clock = clock;
    this.sessionId = null;
    this.lastTransitionAt = startedAt;
  }

  updateSessionId(event) {
    const properties = event?.properties || {};
    const sessionId =
      properties.sessionID || properties.sessionId || this.sessionId || null;
    if (sessionId) this.sessionId = String(sessionId);
    return this.sessionId;
  }

  markTransition() {
    this.lastTransitionAt = this.clock();
  }

  build(state, event) {
    const now = this.clock();
    const attention =
      state === SessionStatus.WAITING ||
      state === SessionStatus.NEEDS_APPROVAL;

    return {
      session_id: this.updateSessionId(event),
      project: this.project,
      state,
      tmux_pane: this.environment.TMUX_PANE || null,
      tmux_socket: this.environment.TMUX || null,
      source_pid: this.processId,
      process_started_at: this.processStartedAt,
      directory: this.directory,
      notification_id: null,
      attention,
      attention_since: attention ? this.lastTransitionAt : null,
      last_transition_ts: this.lastTransitionAt || now,
      preview: state.toLowerCase().replaceAll("_", " "),
      event_type: event?.type || null,
      updated_at: now,
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
  }) {
    this.stateMachine = stateMachine;
    this.eventMapper = eventMapper;
    this.recordBuilder = recordBuilder;
    this.recordWriter = recordWriter;
    this.pendingRequests = new Map();
    this.requestSequence = 0;
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
    const pending = this.pendingRequests.get(sessionId) || new Set();

    if (event?.type === "permission.asked" || event?.type === "permission.updated" || event?.type === "question.asked") {
      pending.add(requestId);
      this.pendingRequests.set(sessionId, pending);
      return;
    }

    if (event?.type !== "permission.replied" && event?.type !== "question.replied" && event?.type !== "question.rejected") {
      return;
    }

    pending.delete(requestId);
    if (pending.size === 0) this.pendingRequests.delete(sessionId);
    else this.pendingRequests.set(sessionId, pending);
  }

  hasPendingRequest(event) {
    const sessionId = this.sessionIdFor(event);
    if (sessionId && this.pendingRequests.get(sessionId)?.size) return true;
    if (!sessionId && this.pendingRequests.size) return true;
    return false;
  }

  async handle(event) {
    const nextState = this.eventMapper.stateFor(event);
    const isIdleEvent =
      event?.type === "session.idle" ||
      (event?.type === "session.status" && nextState === SessionStatus.IDLE);

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

    let changed = false;
    if (nextState && nextState !== this.stateMachine.currentState) {
      if (!this.stateMachine.transitionTo(nextState)) return;
      this.recordBuilder.markTransition();
      changed = true;
    }

    if (
      changed ||
      event?.type === "session.created" ||
      event?.type === "session.updated"
    ) {
      this.recordWriter.write(
        this.recordBuilder.build(this.stateMachine.currentState, event),
      );
    }
  }

  async dispose() {
    this.recordWriter.remove();
  }
}

async function server({ project, directory }) {
  const recordPath = path.join(statusDir, `${process.pid}.json`);
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
  });

  return {
    event: async ({ event }) => reporter.handle(event),
    dispose: async () => reporter.dispose(),
  };
}

export {
  AtomicStatusRecordWriter,
  EventStateMapper,
  LifecycleStateMachine,
  OpenCodeStatusReporter,
  SessionStatus,
  StatusRecordBuilder,
  TransitionPolicy,
};

export default {
  id: "opencode-praefectus-fabrum",
  server,
};
