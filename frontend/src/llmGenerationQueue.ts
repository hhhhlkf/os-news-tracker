/** Client-side queues for on-demand item LLM generation (推荐理由 / OS启发). */

/** At most this many item cards may occupy a generation slot at once. */
export const MAX_CARD_GENERATION_SLOTS = 3;

/** At most this many reason/insight HTTP→LLM calls may run at once. */
export const MAX_LLM_GENERATION_CALLS = 3;

class AsyncSemaphore {
  private permits: number;
  private readonly waiters: Array<() => void> = [];

  constructor(permits: number) {
    this.permits = permits;
  }

  async acquire(): Promise<void> {
    if (this.permits > 0) {
      this.permits -= 1;
      return;
    }
    await new Promise<void>((resolve) => {
      this.waiters.push(resolve);
    });
  }

  release(): void {
    const next = this.waiters.shift();
    if (next) {
      next();
      return;
    }
    this.permits += 1;
  }

  async run<T>(fn: () => Promise<T>): Promise<T> {
    await this.acquire();
    try {
      return await fn();
    } finally {
      this.release();
    }
  }
}

const cardSlots = new AsyncSemaphore(MAX_CARD_GENERATION_SLOTS);
const llmCallSlots = new AsyncSemaphore(MAX_LLM_GENERATION_CALLS);

/** Claim one of the 3 card slots; call the returned release exactly once. */
export async function claimCardGenerationSlot(): Promise<() => void> {
  await cardSlots.acquire();
  let released = false;
  return () => {
    if (released) return;
    released = true;
    cardSlots.release();
  };
}

/** Run one reason/insight network call under the LLM concurrency limit. */
export function runQueuedItemLlmCall<T>(fn: () => Promise<T>): Promise<T> {
  return llmCallSlots.run(fn);
}
