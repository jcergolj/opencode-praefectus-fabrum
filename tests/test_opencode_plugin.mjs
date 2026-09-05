import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(
  new URL("../opencode-plugin/index.js", import.meta.url),
  "utf8",
);
const plugin = await import(`data:text/javascript,${encodeURIComponent(source)}`);

test("lifecycle state machine enforces the complete transition matrix", () => {
  const states = Object.values(plugin.SessionStatus);
  const transitions = {
    IDLE: ["IDLE", "WORKING", "WAITING", "NEEDS_APPROVAL"],
    WORKING: ["WORKING", "IDLE", "NEEDS_APPROVAL", "WAITING"],
    WAITING: ["WAITING", "WORKING", "NEEDS_APPROVAL", "IDLE"],
    NEEDS_APPROVAL: [
      "NEEDS_APPROVAL",
      "WORKING",
      "WAITING",
      "IDLE",
    ],
  };

  for (const currentState of states) {
    for (const targetState of states) {
      const machine = new plugin.LifecycleStateMachine({ initialState: currentState });
      const allowed = transitions[currentState].includes(targetState);

      assert.equal(machine.transitionTo(targetState), allowed);
      assert.equal(
        machine.currentState,
        allowed ? targetState : currentState,
      );
    }
  }
});

test("event mapper translates OpenCode events into domain states", () => {
  const mapper = new plugin.EventStateMapper();

  assert.equal(
    mapper.stateFor({ type: "session.status", properties: { status: "busy" } }),
    plugin.SessionStatus.WORKING,
  );
  assert.equal(
    mapper.stateFor({ type: "session.status", properties: { status: { type: "idle" } } }),
    plugin.SessionStatus.IDLE,
  );
  assert.equal(
    mapper.stateFor({ type: "question.asked" }),
    plugin.SessionStatus.WAITING,
  );
  assert.equal(
    mapper.stateFor({ type: "permission.asked" }),
    plugin.SessionStatus.NEEDS_APPROVAL,
  );
  assert.equal(mapper.stateFor({ type: "session.updated" }), null);
});

test("record builder produces the watcher status contract", () => {
  const times = [20, 30, 40];
  const builder = new plugin.StatusRecordBuilder({
    project: { name: "Demo" },
    directory: "/work/demo",
    processId: 123,
    environment: { TMUX_PANE: "%1", TMUX: "/tmp/tmux" },
    processStartedAt: 10,
    clock: () => times.shift(),
  });

  const idle = builder.build("IDLE", {
    type: "session.created",
    properties: { sessionID: "session-1" },
  });
  builder.markTransition();
  const waiting = builder.build("WAITING", { type: "question.asked" });

  assert.deepEqual(idle, {
    session_id: "session-1",
    project: "Demo",
    state: "IDLE",
    tmux_pane: "%1",
    tmux_socket: "/tmp/tmux",
    source_pid: 123,
    process_started_at: 10,
    directory: "/work/demo",
    notification_id: null,
    attention: false,
    attention_since: null,
    last_transition_ts: 10,
    preview: "idle",
    event_type: "session.created",
    updated_at: 20,
  });
  assert.equal(waiting.session_id, "session-1");
  assert.equal(waiting.state, "WAITING");
  assert.equal(waiting.attention, true);
  assert.equal(waiting.attention_since, 30);
  assert.equal(waiting.last_transition_ts, 30);
  assert.equal(waiting.updated_at, 40);
});

test("reporter coordinates mapping, transitions, records, and disposal", async () => {
  const records = [];
  let removed = 0;
  let transitionMarks = 0;
  const reporter = new plugin.OpenCodeStatusReporter({
    stateMachine: new plugin.LifecycleStateMachine(),
    eventMapper: new plugin.EventStateMapper(),
    recordBuilder: {
      markTransition() {
        transitionMarks += 1;
      },
      build(state, event) {
        return { state, eventType: event.type };
      },
    },
    recordWriter: {
      write(record) {
        records.push(record);
      },
      remove() {
        removed += 1;
      },
    },
  });

  await reporter.handle({
    type: "session.created",
    properties: { info: { id: "session-1" } },
  });
  await reporter.handle({
    type: "session.status",
    properties: { sessionID: "session-1", status: "busy" },
  });
  await reporter.handle({ type: "question.asked" });
  await reporter.handle({
    type: "permission.asked",
    properties: { id: "permission-1", sessionID: "session-1" },
  });
  await reporter.handle({
    type: "permission.replied",
    properties: {
      sessionID: "session-1",
      permissionID: "permission-1",
      response: "reject",
    },
  });
  await reporter.handle({
    type: "session.idle",
    properties: { sessionID: "session-1" },
  });
  await reporter.handle({
    type: "permission.asked",
    properties: { id: "permission-2", sessionID: "session-1" },
  });
  await reporter.dispose();

  assert.deepEqual(records, [
    { state: "IDLE", eventType: "session.created" },
    { state: "WORKING", eventType: "session.status" },
    { state: "WAITING", eventType: "question.asked" },
    { state: "NEEDS_APPROVAL", eventType: "permission.asked" },
    { state: "IDLE", eventType: "permission.replied" },
    { state: "NEEDS_APPROVAL", eventType: "permission.asked" },
  ]);
  assert.equal(transitionMarks, 5);
  assert.equal(removed, 1);
});

test("permission requests remain visible until they are answered", async () => {
  const records = [];
  const reporter = new plugin.OpenCodeStatusReporter({
    stateMachine: new plugin.LifecycleStateMachine(),
    eventMapper: new plugin.EventStateMapper(),
    recordBuilder: {
      markTransition() {},
      build(state) {
        return { state };
      },
    },
    recordWriter: {
      write(record) {
        records.push(record);
      },
      remove() {},
    },
  });

  await reporter.handle({
    type: "permission.updated",
    properties: { id: "permission-1", sessionID: "session-1" },
  });
  await reporter.handle({
    type: "session.status",
    properties: { sessionID: "session-1", status: { type: "idle" } },
  });

  assert.equal(reporter.stateMachine.currentState, plugin.SessionStatus.NEEDS_APPROVAL);
  assert.equal(records.at(-1).state, plugin.SessionStatus.NEEDS_APPROVAL);

  await reporter.handle({
    type: "permission.replied",
    properties: {
      sessionID: "session-1",
      permissionID: "permission-1",
      response: "reject",
    },
  });

  assert.equal(reporter.stateMachine.currentState, plugin.SessionStatus.IDLE);
  assert.equal(records.at(-1).state, plugin.SessionStatus.IDLE);
});
