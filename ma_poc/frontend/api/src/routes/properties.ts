import { Router } from 'express';
import type { IPropertyService } from '../../../services/src/interfaces/IPropertyService.js';
import type { PropertyFilters, SortOptions } from '../../../services/src/types/common.js';
import { validateQuery, propertyQuerySchema, searchSchema, rankedSchema } from '../middleware/validation.js';

export function createPropertyRoutes(propertyService: IPropertyService): Router {
  const router = Router();
  router.get('/', validateQuery(propertyQuerySchema), async (req, res, next) => {
    try {
      const { page, pageSize, search, city, tier, status, sort, dir } = req.query as Record<string, string>;
      const filters: PropertyFilters = {};
      if (search) filters.search = search;
      if (city) filters.cities = city.split(',');
      if (tier) filters.tiers = tier.split(',') as any;
      if (status) filters.statuses = status.split(',') as any;
      const sortOptions: SortOptions | undefined = sort ? { field: sort, direction: (dir as 'asc' | 'desc') || 'asc' } : undefined;
      const result = await propertyService.getProperties(filters, sortOptions, parseInt(page) || 1, parseInt(pageSize) || 25);
      res.json(result);
    } catch (err) { next(err); }
  });
  router.get('/stats', async (_req, res, next) => { try { res.json(await propertyService.getAggregateStats()); } catch (err) { next(err); } });
  router.get('/search', validateQuery(searchSchema), async (req, res, next) => { try { const { q, limit } = req.query as Record<string, string>; res.json(await propertyService.searchProperties(q, parseInt(limit) || 20)); } catch (err) { next(err); } });
  router.get('/ranked', validateQuery(rankedSchema), async (req, res, next) => { try { const { metric, dir, limit } = req.query as Record<string, string>; res.json(await propertyService.getRankedProperties(metric, (dir as 'asc' | 'desc') || 'desc', parseInt(limit) || 10)); } catch (err) { next(err); } });
  router.get('/:id', async (req, res, next) => { try { const p = await propertyService.getPropertyById(req.params.id); if (!p) { res.status(404).json({ error: 'Property not found' }); return; } res.json(p); } catch (err) { next(err); } });
  return router;
}
