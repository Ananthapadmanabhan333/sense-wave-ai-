import { healthSchema, safeParse } from "@/lib/schemas";

const API = process.env.NEXT_PUBLIC_SENSEWAVE_API_URL ?? "http://localhost:8000";

async function getHealth() {
  try {
    const res = await fetch(`${API}/api/health`, { cache: "no-store" });
    if (!res.ok) return null;
    return safeParse(healthSchema, await res.json());
  } catch {
    return null;
  }
}

/**
 * Phase 1 placeholder. It shows the backend's real health and upstream state
 * and nothing else -- there is no fabricated room grid here, because there is
 * no dashboard yet. Phase 2 replaces this page.
 */
export default async function Home() {
  const health = await getHealth();

  return (
    <div className="mx-auto max-w-3xl px-6 py-16">
      <h1 className="text-2xl font-semibold">SenseWave AI</h1>
      <p className="mt-2 text-sm text-neutral-400">
        Monitoring layer over the RuView sensing-server.
      </p>

      <section className="mt-8 rounded-lg border border-neutral-800 bg-neutral-900/50 p-5">
        <h2 className="text-sm font-medium uppercase tracking-wide text-neutral-400">
          Backend
        </h2>
        {health === null ? (
          <p className="mt-3 text-sm text-red-400">
            Cannot reach the SenseWave API at {API}. Nothing is shown rather than
            a stale or invented value.
          </p>
        ) : (
          <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
            <dt className="text-neutral-400">Status</dt>
            <dd>{health.status}</dd>
            <dt className="text-neutral-400">Database</dt>
            <dd>{health.database}</dd>
            <dt className="text-neutral-400">Storage</dt>
            <dd>{health.storage_mode ?? "unknown"}</dd>
            <dt className="text-neutral-400">Upstream connected</dt>
            <dd>{String(health.upstream["connected"] ?? "unknown")}</dd>
            <dt className="text-neutral-400">Frames received</dt>
            <dd>{String(health.upstream["frames_received"] ?? "—")}</dd>
            <dt className="text-neutral-400">Frames dropped</dt>
            <dd>{String(health.upstream["frames_invalid"] ?? "—")}</dd>
            <dt className="text-neutral-400">Live WS clients</dt>
            <dd>{health.ws_clients}</dd>
          </dl>
        )}
      </section>

      <p className="mt-6 text-xs text-neutral-500">
        Dashboard arrives in Phase 2. API docs at{" "}
        <a className="underline" href={`${API}/docs`}>
          {API}/docs
        </a>
        .
      </p>
    </div>
  );
}
