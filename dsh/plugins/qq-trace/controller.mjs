import { RemoteError } from '@deepseek-ai/dsh-typert-protocol';

const DISPOSE = Symbol.dispose ?? Symbol.for('Symbol.dispose');
const READ_ONLY_MESSAGE = 'QQ Bot 会话仅供查看，不能通过 DSH Web 修改。';
const disposedObservations = new WeakSet();

function readOnlyError() {
  return new RemoteError('session/read-only', READ_ONLY_MESSAGE, {});
}

function addressSessionId(address) {
  return address.kind === 'session' ? address.sessionId : address.childSessionId;
}

function disposeObservation(observation) {
  if (!observation || disposedObservations.has(observation)) return;
  disposedObservations.add(observation);
  observation[DISPOSE]?.();
}

function linkedSignal(signal, abortController) {
  const abort = () => abortController.abort(signal?.reason);
  if (signal?.aborted) abort();
  else signal?.addEventListener('abort', abort, { once: true });
  return {
    signal: abortController.signal,
    dispose: () => signal?.removeEventListener('abort', abort),
  };
}

function copyProjections(projections, cursor) {
  if (!projections) return { asOfSeq: cursor, values: {} };
  return structuredClone(projections);
}

function waitForPoll(signal, pollMs) {
  if (signal.aborted) return Promise.resolve();
  return new Promise((resolve) => {
    const finish = () => {
      clearTimeout(timer);
      signal.removeEventListener('abort', finish);
      resolve();
    };
    const timer = setTimeout(finish, pollMs);
    signal.addEventListener('abort', finish, { once: true });
  });
}

function sameEvent(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

async function* followExternal(ctx, nativePage, request, signal, pollMs) {
  const id = addressSessionId(request.address);
  let observation;
  try {
    observation = await ctx.sessionQuery.observeSession(id, { signal, projectionMode: 'all' });
    signal.throwIfAborted();
    let cursor = observation.cursor;
    const page = await nativePage({
      address: request.address,
      throughSeq: cursor,
      maxMessages: request.maxMessages,
      turnWindow: request.turnWindow,
    }, signal);
    let previousEvents = [...observation.events];
    yield {
      type: 'snapshot',
      header: { ...observation.header },
      cursor,
      records: page.records,
      hasMore: page.hasMore,
      projections: copyProjections(observation.projections, cursor),
      ...(request.assistantStream === true ? { assistantStream: { revision: 0 } } : {}),
    };

    let nextSeq = cursor + 1;
    while (!signal.aborted) {
      await waitForPoll(signal, pollMs);
      signal.throwIfAborted();
      const next = await ctx.sessionQuery.observeSession(id, { signal, projectionMode: 'none' });
      try {
        const events = [...next.events];
        if (next.cursor < cursor || events.length < previousEvents.length) {
          throw new Error(`QQ trace source for "${id}" was truncated or rewritten`);
        }
        for (let index = 0; index < previousEvents.length; index++) {
          if (!sameEvent(previousEvents[index], events[index])) {
            throw new Error(`QQ trace source for "${id}" was rewritten at seq ${index}`);
          }
        }
        const entries = [];
        for (let index = nextSeq; index <= next.cursor; index++) {
          const event = events[index];
          if (event?.seq !== index) throw new Error(`QQ trace source for "${id}" skipped seq ${nextSeq}`);
          entries.push({ type: 'event', event });
          nextSeq++;
        }
        previousEvents = events;
        cursor = next.cursor;
        const previous = observation;
        observation = next;
        disposeObservation(previous);
        for (const entry of entries) yield entry;
      } catch (error) {
        disposeObservation(next);
        throw error;
      }
    }
  } finally {
    disposeObservation(observation);
  }
}

/** Install read-only QQ trace routing on the native public SessionController. */
export function installController(ctx, { pollMs = 1000 } = {}) {
  if (!Number.isFinite(pollMs) || pollMs < 1) throw new TypeError('pollMs must be a positive number');
  const controller = ctx.sessionController;
  if (!controller) throw new Error('ctx.sessionController is not available');

  const originals = new Map();
  const external = async (id) => Boolean(id && await ctx.sessionPersistence.sourceFor(id));
  const rejectExternal = async (id) => {
    if (await external(id) || ctx.sessionPersistence.isExternal(id)) throw readOnlyError();
  };
  const wrap = (name, replacement) => {
    originals.set(name, Object.getOwnPropertyDescriptor(controller, name));
    controller[name] = replacement;
  };

  const originalPage = controller.page.bind(controller);
  const originalFollow = controller.follow.bind(controller);
  const activeStreams = new Map();
  wrap('follow', function (request, signal) {
    const id = addressSessionId(request.address);
    return (async function* () {
      const streamAbort = new AbortController();
      const linked = linkedSignal(signal, streamAbort);
      activeStreams.set(streamAbort, false);
      try {
        const isExternal = await external(id) || ctx.sessionPersistence.isExternal(id);
        if (isExternal) {
          activeStreams.set(streamAbort, true);
          if (disposed) streamAbort.abort(new Error('QQ trace adapter disposed'));
        }
        if (!isExternal) {
          linked.signal.throwIfAborted();
          yield* originalFollow(request, linked.signal);
          return;
        }
        yield* followExternal(ctx, originalPage, request, linked.signal, pollMs);
      } finally {
        activeStreams.delete(streamAbort);
        linked.dispose();
      }
    })();
  });

  for (const [name, getId] of [
    ['create', (request) => request.sessionId],
    ['selectModel', (request) => request.sessionId],
    ['rename', (request) => request.sessionId],
    ['fork', (request) => request.sessionId],
    ['prompt', (request) => request.sessionId],
    ['updateQueue', (request) => request.sessionId],
  ]) {
    const original = controller[name].bind(controller);
    wrap(name, async function (request, ...args) {
      const id = getId(request);
      await rejectExternal(id);
      // Final synchronous check closes the microtask window between the
      // asynchronous lookup and delegation to the mutable native controller.
      if (ctx.sessionPersistence.isExternal(id)) throw readOnlyError();
      return original(request, ...args);
    });
  }

  const originalCancel = controller.cancel.bind(controller);
  wrap('cancel', function (request, ...args) {
    if (ctx.sessionPersistence.isExternal(request.sessionId)) throw readOnlyError();
    return originalCancel(request, ...args);
  });

  const originalResolveAgent = controller.resolveAgent.bind(controller);
  wrap('resolveAgent', async function (id) {
    if (await external(id) || ctx.sessionPersistence.isExternal(id)) return { error: readOnlyError() };
    return originalResolveAgent(id);
  });

  let disposed = false;
  const dispose = () => {
    if (disposed) return;
    disposed = true;
    for (const [stream, isExternal] of activeStreams) {
      if (isExternal) stream.abort(new Error('QQ trace adapter disposed'));
    }
    for (const [name, descriptor] of originals) {
      if (descriptor) Object.defineProperty(controller, name, descriptor);
      else delete controller[name];
    }
  };
  ctx.effect(() => dispose, 'qq-trace: session controller adapter');
  return dispose;
}
