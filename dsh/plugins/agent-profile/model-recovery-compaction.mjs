import BasicCompactionEngine from '@deepseek-ai/dsh-compaction-basic'
import { LlmError } from '@deepseek-ai/dsh-llm'

const INSTRUCTION_BUDGET = 1800

function targetFor(agent) {
  const current = agent.session.requestHeader()?.config
  if (current?.provider && current?.model) return current
  if (agent.options?.provider && agent.options?.model) return agent.options
}

function activeTurn(agent) {
  const events = agent.session.snapshotEvents()
  for (let i = events.length - 1; i >= 0; i--) {
    const event = events[i]
    if (event.type === 'turn/start') return Number.isInteger(event.data?.turn) ? event.data.turn : undefined
    if (event.type === 'turn/end') return undefined
  }
}

function classify(policy, failure) {
  return policy?.classifyFailure?.(failure) ?? { code: failure?.failure?.code ?? failure?.code ?? 'MODEL_ERROR',
    status: failure?.failure?.status, eligible: false }
}

function effectiveConfig(engine, target) {
  if (!target) return engine.config
  const override = engine.config.modelPolicies?.find(p => p.provider === target.provider && p.model === target.model)
  return {
    ...engine.config,
    ...override,
    modelPolicies: engine.config.modelPolicies,
  }
}

function fallbackConfig(config, target, backup) {
  const policies = (config.modelPolicies ?? []).map(p => ({ ...p }))
  const scoped = policies.find(p => p.provider === target?.provider && p.model === target?.model)
  if (scoped) {
    scoped.summarizationProvider = backup.provider
    scoped.summarizationModel = backup.model
  }
  return { ...config, summarizationProvider: backup.provider, summarizationModel: backup.model,
    modelPolicies: policies }
}

export default class ModelRecoveryCompaction extends BasicCompactionEngine {
  async summarize(input, agent, signal) {
    const target = targetFor(agent)
    const policy = this.ctx.reflect?.get?.('v2ModelRecovery')
    const config = effectiveConfig(this, target)
    const requested = config.summarizationProvider
      ? { provider: config.summarizationProvider, model: config.summarizationModel }
      : target
    try {
      return await super.summarize(input, agent, signal)
    } catch (primaryError) {
      if (!(primaryError instanceof LlmError) || signal?.aborted) throw primaryError
      const classified = classify(policy, primaryError.failure)
      if (!classified.eligible || !requested?.provider || !requested?.model) throw primaryError
      const attempted = [requested]
      let failure = primaryError
      for (let attempt = 0; attempt < 2; attempt++) {
      const backup = policy?.fallbackFor?.(attempted.at(-1), failure.failure, attempted)
      if (!backup) throw failure
      attempted.push(backup)
      try {
        if (signal?.aborted) throw new LlmError('summarization cancelled', 'CANCELLED')
        const info = await this.ctx.llm.resolveModelInfo(backup.provider, backup.model, signal)
        if (info.provider !== backup.provider || info.id !== backup.model || !Number.isInteger(info.context?.contextWindow)) {
          throw new Error('backup model capacity unavailable')
        }
        const prepared = await this.ctx.llm.prepareCall({ provider: backup.provider, model: backup.model,
          maxTokens: config.maxTokens }, signal)
        const actualMax = prepared.config?.maxTokens ?? config.maxTokens
        if (!Number.isInteger(actualMax) || actualMax <= 0 || actualMax >= info.context.contextWindow) {
          throw new Error('backup output budget exceeds model capacity')
        }
        const meter = this.ctx.tokenMeter
        const inputTokens = input.messages.reduce((sum, message) => sum + meter.estimateMessage(message), 0)
          + Math.ceil((JSON.stringify(input.tools ?? []).length + JSON.stringify(agent.session.toolHistory() ?? []).length) / 2)
          + INSTRUCTION_BUDGET
        if (inputTokens + actualMax > info.context.contextWindow) throw new Error('complete summary input exceeds backup capacity')
        if (signal?.aborted) throw new LlmError('summarization cancelled', 'CANCELLED')

        const facade = Object.create(this)
        facade.config = fallbackConfig(config, target, backup)
        const result = await BasicCompactionEngine.prototype.summarize.call(facade, input, agent, signal)
        delete result.usage
        const turn = activeTurn(agent)
        if (turn !== undefined) policy.record?.(agent, turn, { requested_provider: requested.provider,
          requested_model: requested.model, ...backup, code: classified.code, status: classified.status,
          attempted: true, recovered: true })
        return result
      } catch (fallbackError) {
        if (signal?.aborted) throw fallbackError
        failure = fallbackError
        // Only API failures may move to the next distinct route. Capacity,
        // preparation and validation failures end this attempt immediately.
        if (fallbackError instanceof LlmError && attempt === 0
            && policy?.fallbackFor?.(backup, fallbackError.failure, attempted)) continue
        const turn = activeTurn(agent)
        if (turn !== undefined) policy.record?.(agent, turn, { requested_provider: requested.provider,
          requested_model: requested.model, ...backup, code: classified.code, status: classified.status,
          attempted: true, recovered: false })
        const safe = classify(policy, fallbackError instanceof LlmError ? fallbackError.failure : {})
        throw new LlmError('model summarization failed on primary and backup routes', safe.code,
          safe.status ? { status: safe.status } : undefined)
      }
      }
    }
  }
}
