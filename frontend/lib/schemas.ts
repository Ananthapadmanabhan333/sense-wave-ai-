/**
 * Zod schemas for everything crossing the wire.
 *
 * Every API response and every WebSocket message is parsed here at the
 * boundary. A malformed payload fails one card, never the app.
 *
 * The vitals shape mirrors the backend deliberately: a rate never arrives
 * without its confidence and the server's low-confidence verdict, so a
 * component cannot render a bare number by accident (product rule 1).
 */

import { z } from "zod";

export const vitalsSchema = z.object({
  breathing_rate_bpm: z.number().nullable(),
  breathing_confidence: z.number().nullable(),
  breathing_is_low_confidence: z.boolean(),
  heart_rate_bpm: z.number().nullable(),
  heartbeat_confidence: z.number().nullable(),
  heartbeat_is_low_confidence: z.boolean(),
  signal_quality: z.number().nullable(),
  suppressed: z.boolean(),
});
export type Vitals = z.infer<typeof vitalsSchema>;

export const nodeSchema = z.object({
  node_id: z.number(),
  name: z.string().nullable(),
  room_id: z.number().nullable(),
  rssi_dbm: z.number().nullable(),
  frame_rate_hz: z.number().nullable(),
  novelty_score: z.number().nullable(),
  last_seen: z.string().nullable(),
  upstream_stale: z.boolean(),
  stale: z.boolean(),
});
export type SensorNode = z.infer<typeof nodeSchema>;

export const roomSchema = z.object({
  id: z.number(),
  site_id: z.number(),
  name: z.string(),
  monitoring_enabled: z.boolean(),
  privacy_mode: z.boolean(),
  node_count: z.number(),
});
export type Room = z.infer<typeof roomSchema>;

export const readingSchema = z.object({
  time: z.string(),
  presence: z.boolean().nullable(),
  presence_confidence: z.number().nullable(),
  motion_level: z.string().nullable(),
  motion_band_power: z.number().nullable(),
  breathing_band_power: z.number().nullable(),
  mean_rssi: z.number().nullable(),
  estimated_persons: z.number().nullable(),
  vitals: vitalsSchema,
});

export const roomLatestSchema = z.object({
  room: roomSchema,
  reading: readingSchema.nullable(),
  nodes: z.array(nodeSchema),
  stale: z.boolean(),
  stale_reason: z.string().nullable(),
});
export type RoomLatest = z.infer<typeof roomLatestSchema>;

export const healthSchema = z.object({
  status: z.string(),
  database: z.string(),
  storage_mode: z.string().nullable(),
  upstream: z.record(z.unknown()),
  ws_clients: z.number(),
});
export type Health = z.infer<typeof healthSchema>;

/** A point may be null -- that is a real gap and must render as one. */
export const historyPointSchema = z.object({
  time: z.string(),
  value: z.number().nullable(),
  confidence: z.number().nullable(),
});

export const historySchema = z.object({
  room_id: z.number(),
  metric: z.string(),
  min_confidence: z.number().nullable(),
  points: z.array(historyPointSchema),
});

/** Messages arriving on /api/ws/live. */
export const liveMessageSchema = z.discriminatedUnion("type", [
  z.object({ type: z.literal("connected"), server_time: z.string() }),
  z.object({
    type: z.literal("reading"),
    room_id: z.number(),
    timestamp: z.number(),
    source: z.string(),
    tick: z.number(),
    presence: z.boolean(),
    presence_confidence: z.number(),
    motion_level: z.string(),
    motion_band_power: z.number(),
    mean_rssi: z.number(),
    estimated_persons: z.number().nullable(),
    vitals_suppressed: z.boolean(),
    vitals: vitalsSchema
      .pick({
        breathing_rate_bpm: true,
        breathing_confidence: true,
        heart_rate_bpm: true,
        heartbeat_confidence: true,
        signal_quality: true,
      })
      .nullable(),
    nodes: z.array(
      z.object({
        node_id: z.number(),
        rssi_dbm: z.number(),
        frame_rate_hz: z.number(),
        novelty_score: z.number(),
        last_seen_ms: z.number(),
        stale: z.boolean(),
      }),
    ),
  }),
]);
export type LiveMessage = z.infer<typeof liveMessageSchema>;

/**
 * Parse without throwing. Returns null on a malformed payload so a caller can
 * degrade one card instead of white-screening the app.
 */
export function safeParse<T>(schema: z.ZodType<T>, data: unknown): T | null {
  const result = schema.safeParse(data);
  if (!result.success) {
    console.warn("sensewave: dropped malformed payload", result.error.issues);
    return null;
  }
  return result.data;
}
