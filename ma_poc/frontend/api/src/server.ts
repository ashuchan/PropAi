import express from 'express';
import cors from 'cors';
import { existsSync } from 'fs';
import { join } from 'path';
import { config } from './config.js';
import { createRequestLogger } from './middleware/requestLogger.js';
import { errorHandler } from './middleware/errorHandler.js';
import { createPropertyRoutes } from './routes/properties.js';
import { createRunRoutes } from './routes/runs.js';
import { createDiffRoutes } from './routes/diff.js';
import { createHealthRoutes } from './routes/health.js';
import { createFloorPlanComparisonRoutes } from './routes/floorPlanComparisons.js';
import { createAdminRoutes } from './routes/admin.js';

async function startServer() {
  const { createServices } = await import('../../../services/src/factory.js');
  // Provider kind (filesystem vs postgres) is resolved inside createServices
  // from the DATA_PROVIDER env var. Postgres provider construction is async
  // so it can resolve IAM auth through the Cloud SQL Connector when
  // CLOUD_SQL_INSTANCE is set — server.ts stays backend-agnostic.
  const services = await createServices({ dataDir: config.dataDir });
  const providerName = services.dataProvider.name;

  const app = express();
  app.use(cors({ origin: config.corsOrigin }));
  app.use(express.json());
  app.use(createRequestLogger(config.logLevel));
  app.get('/api/config', (_req, res) => {
    res.json({ schemaVersion: config.schemaVersion, dataProvider: providerName });
  });
  app.use('/api/properties', createPropertyRoutes(services.properties));
  app.use('/api/runs', createRunRoutes(services.runs));
  app.use('/api/diff', createDiffRoutes(services.diff));
  app.use('/api/health', createHealthRoutes(services.health));
  app.use(
    '/api/floor-plan-comparisons',
    createFloorPlanComparisonRoutes(services.floorPlanComparisons),
  );
  // Admin endpoints — operator-triggered scripts (email reports, etc.).
  // No auth on this route group; gate at the deployment / VPC layer.
  app.use('/api/admin', createAdminRoutes());

  // SPA serving — single-container Cloud Run deploy. Mounted AFTER /api/* so
  // route ordering can never shadow an API path. The catch-all SPA fallback
  // (`/*` → index.html) is required because React Router uses HTML5 history
  // and a hard refresh on /properties/X must still hand back the SPA shell.
  if (config.serveStatic) {
    if (!existsSync(config.staticDir)) {
      console.warn(`[static] SERVE_STATIC=true but ${config.staticDir} not found — disabling`);
    } else {
      console.log(`[static] serving SPA from ${config.staticDir}`);
      // immutable hashed assets: long cache; index.html: no-cache so the
      // user always gets the latest entry point after a deploy.
      app.use(
        express.static(config.staticDir, {
          maxAge: '1y',
          immutable: true,
          index: false,
        }),
      );
      app.get('*', (req, res, next) => {
        if (req.path.startsWith('/api/')) return next();
        res.sendFile(join(config.staticDir, 'index.html'), {
          headers: { 'Cache-Control': 'no-cache' },
        });
      });
    }
  }

  app.use(errorHandler);

  const server = app.listen(config.port, () => {
    console.log(`API server listening on http://localhost:${config.port}`);
    console.log(`Data provider: ${providerName}`);
    console.log(`Data directory: ${config.dataDir}`);
  });

  // Pre-warm the property summary cache. The first /api/properties* call
  // triggers a multi-table aggregate over Cloud SQL; doing it now means
  // the user's first dashboard load is a cache hit, not a 1-2s wait.
  // Errors are logged but never block startup — a cold cache on Cloud
  // SQL outage shouldn't take the API down.
  void services.properties.getAggregateStats().catch((err) => {
    console.warn('[startup] property cache pre-warm failed:', err.message);
  });

  const shutdown = async (signal: string) => {
    console.log(`\nReceived ${signal}, closing server…`);
    server.close();
    await services.dataProvider.close();
    process.exit(0);
  };
  process.on('SIGINT', () => void shutdown('SIGINT'));
  process.on('SIGTERM', () => void shutdown('SIGTERM'));
}

startServer().catch((err) => {
  console.error('Failed to start server:', err);
  process.exit(1);
});
