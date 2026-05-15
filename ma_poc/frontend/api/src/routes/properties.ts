import { Router } from 'express';
import type { IPropertyService } from '../../../services/src/interfaces/IPropertyService.js';
import type { PropertyFilters, SortOptions, DataScope } from '../../../services/src/types/common.js';
import { validateQuery, propertyQuerySchema, searchSchema, rankedSchema, idQuerySchema } from '../middleware/validation.js';

// `scope` is validated + defaulted by zod (`scopeSchema`) before handlers
// run, so handlers can read `req.query.scope` directly without
// re-normalising or type-casting.

export function createPropertyRoutes(propertyService: IPropertyService): Router {
  const router = Router();
  router.get('/', validateQuery(propertyQuerySchema), async (req, res, next) => {
    try {
      const { page, pageSize, search, city, tier, status, sort, dir, scope } = req.query as Record<string, string>;
      const filters: PropertyFilters = {};
      if (search) filters.search = search;
      if (city) filters.cities = city.split(',');
      if (tier) filters.tiers = tier.split(',') as any;
      if (status) filters.statuses = status.split(',') as any;
      const sortOptions: SortOptions | undefined = sort ? { field: sort, direction: (dir as 'asc' | 'desc') || 'asc' } : undefined;
      const result = await propertyService.getProperties(filters, sortOptions, parseInt(page) || 1, parseInt(pageSize) || 25, scope as DataScope);
      res.json(result);
    } catch (err) { next(err); }
  });
  router.get('/stats', validateQuery(idQuerySchema), async (req, res, next) => {
    try {
      const { scope } = req.query as Record<string, string>;
      res.json(await propertyService.getAggregateStats({}, scope as DataScope));
    } catch (err) { next(err); }
  });
  router.get('/search', validateQuery(searchSchema), async (req, res, next) => {
    try {
      const { q, limit, scope } = req.query as Record<string, string>;
      res.json(await propertyService.searchProperties(q, parseInt(limit) || 20, scope as DataScope));
    } catch (err) { next(err); }
  });
  router.get('/ranked', validateQuery(rankedSchema), async (req, res, next) => {
    try {
      const { metric, dir, limit, scope } = req.query as Record<string, string>;
      res.json(await propertyService.getRankedProperties(metric, (dir as 'asc' | 'desc') || 'desc', parseInt(limit) || 10, scope as DataScope));
    } catch (err) { next(err); }
  });
  router.get('/:id', validateQuery(idQuerySchema), async (req, res, next) => {
    try {
      const { scope } = req.query as Record<string, string>;
      const p = await propertyService.getPropertyById(req.params.id, scope as DataScope);
      if (!p) { res.status(404).json({ error: 'Property not found' }); return; }
      res.json(p);
    } catch (err) { next(err); }
  });
  router.get('/:id/report', async (req, res, next) => { try { const r = await propertyService.getPropertyReport(req.params.id); if (!r) { res.status(404).json({ error: 'Report not found' }); return; } res.json(r); } catch (err) { next(err); } });
  router.get('/:id/profile', async (req, res, next) => { try { const p = await propertyService.getPropertyProfile(req.params.id); if (!p) { res.status(404).json({ error: 'Profile not found' }); return; } res.json(p); } catch (err) { next(err); } });
  return router;
}
