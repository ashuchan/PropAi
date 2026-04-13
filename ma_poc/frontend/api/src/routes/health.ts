import { Router } from 'express';
import type { IHealthService } from '../../../services/src/interfaces/IHealthService.js';
export function createHealthRoutes(healthService: IHealthService): Router {
  const router = Router();
  router.get('/', async (_req, res, next) => { try { res.json(await healthService.getHealthSummary()); } catch (err) { next(err); } });
  router.get('/tiers', async (_req, res, next) => { try { res.json(await healthService.getTierDistribution()); } catch (err) { next(err); } });
  router.get('/failures', async (_req, res, next) => { try { res.json(await healthService.getTopFailures()); } catch (err) { next(err); } });
  router.get('/identity', async (_req, res, next) => { try { res.json(await healthService.getEntityResolutionStats()); } catch (err) { next(err); } });
  return router;
}
