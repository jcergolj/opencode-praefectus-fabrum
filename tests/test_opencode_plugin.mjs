import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const pluginSource = await readFile(
  new URL("../opencode-plugin/index.js", import.meta.url),
  "utf8",
);
const plugin = await import(`data:text/javascript,${encodeURIComponent(pluginSource)}`);

test("lifecycle state machine enforces the complete transition matrix", () => {
  const sessionStatuses = Object.values(plugin.SessionStatus);
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

  for (const currentState of sessionStatuses) {
    for (const targetState of sessionStatuses) {
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
  const stateMapper = new plugin.EventStateMapper();

  assert.equal(
    stateMapper.stateFor({ type: "session.status", properties: { status: "busy" } }),
    plugin.SessionStatus.WORKING,
  );
  assert.equal(
    stateMapper.stateFor({ type: "session.status", properties: { status: { type: "idle" } } }),
    plugin.SessionStatus.IDLE,
  );
  assert.equal(
    stateMapper.stateFor({ type: "question.asked" }),
    plugin.SessionStatus.WAITING,
  );
  assert.equal(
    stateMapper.stateFor({ type: "permission.asked" }),
    plugin.SessionStatus.NEEDS_APPROVAL,
  );
  assert.equal(stateMapper.stateFor({ type: "session.updated" }), null);
});

test("context usage helpers calculate tokens and resolve model limits", () => {
  assert.equal(
    plugin.contextTokensFor({
      tokens: {
        input: 100,
        output: 20,
        reasoning: 5,
        cache: { read: 30, write: 10 },
      },
    }),
    165,
  );
  assert.equal(
    plugin.contextTokensFor({ tokens: { total: 42, input: 100 } }),
    42,
  );
  assert.equal(plugin.contextTokensFor({}), null);
  assert.equal(
    plugin.contextLimitFor(
      [
        {
          id: "demo",
          models: { "model-1": { limit: { context: 8192 } } },
        },
      ],
      "demo",
      "model-1",
    ),
    8192,
  );
  assert.equal(plugin.contextLimitFor([], "demo", "model-1"), null);
});

test("context limit resolver caches provider metadata", async () => {
  let configuredCalls = 0;
  let allCalls = 0;
  const resolver = new plugin.ContextLimitResolver({
    client: {
      config: {
        providers: async () => {
          configuredCalls += 1;
          return {
            data: {
              providers: [
                {
                  id: "demo",
                  models: { "model-1": { limit: { context: 4096 } } },
                },
              ],
            },
          };
        },
      },
      provider: {
        list: async () => {
          allCalls += 1;
          return { data: { all: [] } };
        },
      },
    },
  });

  assert.equal(
    await resolver.limitFor({ providerID: "demo", modelID: "model-1" }),
    4096,
  );
  assert.equal(
    await resolver.limitFor({ providerID: "demo", modelID: "model-1" }),
    4096,
  );
  assert.equal(configuredCalls, 1);
  assert.equal(allCalls, 1);
});

test("record builder produces the watcher status contract", () => {
  const timestamps = [20, 30, 40];
  const builder = new plugin.StatusRecordBuilder({
    project: { name: "Demo" },
    directory: "/work/demo",
    processId: 123,
    environment: { TMUX_PANE: "%1", TMUX: "/tmp/tmux" },
    processStartedAt: 10,
    clock: () => timestamps.shift(),
  });

  const idleRecord = builder.build("IDLE", {
    type: "session.created",
    properties: { sessionID: "session-1" },
  });
  builder.markTransition();
  const waitingRecord = builder.build("WAITING", { type: "question.asked" });

  assert.deepEqual(idleRecord, {
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
  assert.equal(waitingRecord.session_id, "session-1");
  assert.equal(waitingRecord.state, "WAITING");
  assert.equal(waitingRecord.attention, true);
  assert.equal(waitingRecord.attention_since, 30);
  assert.equal(waitingRecord.last_transition_ts, 30);
  assert.equal(waitingRecord.updated_at, 40);
});

test("reporter writes a baseline record before the first session event", async () => {
  const writtenRecords = [];
  const builder = new plugin.StatusRecordBuilder({
    project: { name: "Demo" },
    directory: "/work/demo",
    processId: 123,
    environment: {},
    processStartedAt: 10,
    clock: () => 20,
  });
  const reporter = new plugin.OpenCodeStatusReporter({
    stateMachine: new plugin.LifecycleStateMachine(),
    eventMapper: new plugin.EventStateMapper(),
    recordBuilder: builder,
    recordWriter: {
      write(record) {
        writtenRecords.push(record);
      },
      remove() {},
    },
  });

  await reporter.initialize();

  assert.deepEqual(writtenRecords, [
    {
      session_id: "pid:123",
      project: "Demo",
      state: "IDLE",
      tmux_pane: null,
      tmux_socket: null,
      source_pid: 123,
      process_started_at: 10,
      directory: "/work/demo",
      notification_id: null,
      attention: false,
      attention_since: null,
      last_transition_ts: 10,
      preview: "idle",
      event_type: "server.connected",
      updated_at: 20,
    },
  ]);
});

test("record builder includes the latest context percentage", () => {
  const builder = new plugin.StatusRecordBuilder({
    project: { name: "Demo" },
    directory: "/work/demo",
    processId: 123,
    environment: {},
    processStartedAt: 10,
    clock: () => 20,
  });
  const info = {
    sessionID: "session-1",
    role: "assistant",
    tokens: {
      input: 400,
      output: 50,
      reasoning: 25,
      cache: { read: 20, write: 5 },
    },
  };

  assert.equal(builder.updateContextUsage(info, 1000), true);
  assert.deepEqual(
    builder.build("WORKING", {
      type: "message.updated",
      properties: { info },
    }),
    {
      session_id: "session-1",
      project: "Demo",
      state: "WORKING",
      tmux_pane: null,
      tmux_socket: null,
      source_pid: 123,
      process_started_at: 10,
      directory: "/work/demo",
      notification_id: null,
      attention: false,
      attention_since: null,
      last_transition_ts: 10,
      preview: "working",
      event_type: "message.updated",
      updated_at: 20,
      context_tokens: 500,
      context_limit: 1000,
      context_percentage: 50,
    },
  );
});

test("reporter writes assistant context usage updates", async () => {
  const writtenRecords = [];
  const builder = new plugin.StatusRecordBuilder({
    project: { name: "Demo" },
    directory: "/work/demo",
    processId: 123,
    processStartedAt: 10,
    clock: () => 20,
  });
  const reporter = new plugin.OpenCodeStatusReporter({
    stateMachine: new plugin.LifecycleStateMachine(),
    eventMapper: new plugin.EventStateMapper(),
    recordBuilder: builder,
    recordWriter: {
      write(record) {
        writtenRecords.push(record);
      },
      remove() {},
    },
    contextLimitFor: async () => 2000,
  });

  await reporter.handle({
    type: "message.updated",
    properties: {
      info: {
        sessionID: "session-1",
        role: "assistant",
        providerID: "demo",
        modelID: "model-1",
        tokens: {
          input: 900,
          output: 50,
          reasoning: 25,
          cache: { read: 20, write: 5 },
        },
      },
    },
  });

  assert.equal(writtenRecords.length, 1);
  assert.equal(writtenRecords[0].context_percentage, 50);
});

test("reporter records an initial idle status from a restored session", async () => {
  const writtenRecords = [];
  const reporter = new plugin.OpenCodeStatusReporter({
    stateMachine: new plugin.LifecycleStateMachine(),
    eventMapper: new plugin.EventStateMapper(),
    recordBuilder: {
      markTransition() {},
      build(sessionState, event) {
        return { state: sessionState, eventType: event.type };
      },
    },
    recordWriter: {
      write(record) {
        writtenRecords.push(record);
      },
      remove() {},
    },
  });

  await reporter.handle({
    type: "session.status",
    properties: { sessionID: "session-1", status: { type: "idle" } },
  });

  assert.deepEqual(writtenRecords, [
    { state: plugin.SessionStatus.IDLE, eventType: "session.status" },
  ]);
});

test("permission records include the requested operation in their preview", () => {
  const builder = new plugin.StatusRecordBuilder({
    project: { name: "Demo" },
    directory: "/work/demo",
    processId: 123,
    processStartedAt: 10,
    clock: () => 20,
  });

  const permissionRecord = builder.build("NEEDS_APPROVAL", {
    type: "permission.updated",
    properties: {
      sessionID: "session-1",
      permission: "edit",
      patterns: ["src/app.js"],
    },
  });

  assert.equal(permissionRecord.preview, "edit: src/app.js");
});

test("reporter coordinates mapping, transitions, records, and disposal", async () => {
  const writtenRecords = [];
  let removeCallCount = 0;
  let transitionMarkCount = 0;
  const reporter = new plugin.OpenCodeStatusReporter({
    stateMachine: new plugin.LifecycleStateMachine(),
    eventMapper: new plugin.EventStateMapper(),
    recordBuilder: {
      markTransition() {
        transitionMarkCount += 1;
      },
      build(sessionState, event) {
        return { state: sessionState, eventType: event.type };
      },
    },
    recordWriter: {
      write(record) {
        writtenRecords.push(record);
      },
      remove() {
        removeCallCount += 1;
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

  assert.deepEqual(writtenRecords, [
    { state: "IDLE", eventType: "session.created" },
    { state: "WORKING", eventType: "session.status" },
    { state: "WAITING", eventType: "question.asked" },
    { state: "NEEDS_APPROVAL", eventType: "permission.asked" },
    { state: "IDLE", eventType: "permission.replied" },
    { state: "NEEDS_APPROVAL", eventType: "permission.asked" },
  ]);
  assert.equal(transitionMarkCount, 5);
  assert.equal(removeCallCount, 1);
});

test("permission requests remain visible until they are answered", async () => {
  const writtenRecords = [];
  const reporter = new plugin.OpenCodeStatusReporter({
    stateMachine: new plugin.LifecycleStateMachine(),
    eventMapper: new plugin.EventStateMapper(),
    recordBuilder: {
      markTransition() {},
      build(sessionState) {
        return { state: sessionState };
      },
    },
    recordWriter: {
      write(record) {
        writtenRecords.push(record);
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
  assert.equal(writtenRecords.at(-1).state, plugin.SessionStatus.NEEDS_APPROVAL);

  await reporter.handle({
    type: "permission.replied",
    properties: {
      sessionID: "session-1",
      permissionID: "permission-1",
      response: "reject",
    },
  });

  assert.equal(reporter.stateMachine.currentState, plugin.SessionStatus.IDLE);
  assert.equal(writtenRecords.at(-1).state, plugin.SessionStatus.IDLE);
});

test("permission updates refresh the expanded preview", async () => {
  const writtenRecords = [];
  const reporter = new plugin.OpenCodeStatusReporter({
    stateMachine: new plugin.LifecycleStateMachine(),
    eventMapper: new plugin.EventStateMapper(),
    recordBuilder: {
      markTransition() {},
      build(sessionState, event) {
        return { state: sessionState, eventType: event.type };
      },
    },
    recordWriter: {
      write(record) {
        writtenRecords.push(record);
      },
      remove() {},
    },
  });

  await reporter.handle({
    type: "permission.asked",
    properties: { id: "permission-1", sessionID: "session-1" },
  });
  await reporter.handle({
    type: "permission.updated",
    properties: { id: "permission-1", sessionID: "session-1" },
  });

  assert.deepEqual(writtenRecords.map((record) => record.eventType), [
    "permission.asked",
    "permission.updated",
  ]);
});
