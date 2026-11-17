/**
 * TypeScript Client SDK for Weirwise Token Router.
 */

export interface RouteChatOptions {
  messages: Array<{ role: string; content: string }>;
  provider?: string;
  model?: string;
  strategy?: "priority" | "round_robin" | "weighted" | "least_recently_used" | "lowest_cost";
  maxFailoverAttempts?: number;
  [key: string]: any;
}

export interface RouteResponse {
  provider: string;
  model: string;
  credential_id: string;
  status: string;
  status_code: number;
  latency_ms: number;
  usage: {
    input_tokens: number;
    output_tokens: number;
    total_tokens: number;
  };
  estimated_cost_usd: string;
  failover_attempts: number;
  data: any;
}

export class TokenRouterClient {
  private baseUrl: string;
  private consumerApiKey: string;
  private timeoutMs: number;

  constructor(config: { baseUrl: string; consumerApiKey: string; timeoutMs?: number }) {
    this.baseUrl = config.baseUrl.replace(/\/$/, "");
    this.consumerApiKey = config.consumerApiKey;
    this.timeoutMs = config.timeoutMs || 30000;
  }

  async routeChat(options: RouteChatOptions): Promise<RouteResponse> {
    const { messages, provider, model = "auto", strategy, maxFailoverAttempts = 3, ...extra } = options;

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);

    try {
      const response = await fetch(`${this.baseUrl}/api/v1/route`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Consumer-Key": this.consumerApiKey,
        },
        body: JSON.stringify({
          provider,
          capability: "chat",
          model,
          strategy,
          max_failover_attempts: maxFailoverAttempts,
          request: { messages, ...extra },
        }),
        signal: controller.signal,
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(`TokenRouter Error (${response.status}): ${JSON.stringify(errorData)}`);
      }

      return (await response.json()) as RouteResponse;
    } finally {
      clearTimeout(timer);
    }
  }
}
