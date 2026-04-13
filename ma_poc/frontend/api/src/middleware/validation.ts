import { z } from 'zod';
import type { Request, Response, NextFunction } from 'express';
export const propertyQuerySchema = z.object({ page: z.coerce.number().int().positive().optional().default(1), pageSize: z.coerce.number().int().positive().max(100).optional().default(25), search: z.string().optional(), city: z.string().optional(), tier: z.string().optional(), status: z.string().optional(), sort: z.string().optional(), dir: z.enum(['asc', 'desc']).optional().default('asc') }).passthrough();
export const limitSchema = z.object({ limit: z.coerce.number().int().positive().max(100).optional().default(20) });
export const searchSchema = z.object({ q: z.string().min(1), limit: z.coerce.number().int().positive().max(100).optional().default(20) });
export const rankedSchema = z.object({ metric: z.string(), dir: z.enum(['asc', 'desc']).optional().default('desc'), limit: z.coerce.number().int().positive().max(100).optional().default(10) });
export function validateQuery(schema: z.ZodType) {
  return (req: Request, res: Response, next: NextFunction) => {
    const result = schema.safeParse(req.query);
    if (!result.success) { res.status(400).json({ error: 'Invalid query parameters', details: result.error.issues.map((i: any) => `${i.path.join('.')}: ${i.message}`) }); return; }
    req.query = result.data; next();
  };
}
