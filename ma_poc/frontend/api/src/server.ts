import express from 'express';
import cors from 'cors';
import { config } from './config.js';
import { createRequestLogger } from './middleware/requestLogger.js';
import { errorHandler } from './middleware/errorHandler.js';
import { createPropertyRoutes } from './routes/properties.js';
import { createRunRoutes } from './routes/runs.js';
import { createDiffRoutes } from './routes/diff.js';
import { createHealthRoutes } from './routes/health.js';

async function startServer() {
  const { createServices } = await import('../../../services/src/factory.js');
  // Provider kind (filesystem vs postgres) is resolved inside createServices
  // from the DATA_PROVIDER env var. server.ts stays backend-agnostic.
  const services = createServices({ dataDir: config.dataDir });
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
  app.use(errorHandler);

  const server = app.listen(config.port, () => {
    console.log(`API server listening on http://localhost:${config.port}`);
    console.log(`Data provider: ${providerName}`);
    console.log(`Data directory: ${config.dataDir}`);
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
