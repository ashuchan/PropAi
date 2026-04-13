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
  const services = createServices({ implementation: 'json-file', dataDir: config.dataDir });
  const app = express();
  app.use(cors({ origin: config.corsOrigin }));
  app.use(express.json());
  app.use(createRequestLogger(config.logLevel));
  app.use('/api/properties', createPropertyRoutes(services.properties));
  app.use('/api/runs', createRunRoutes(services.runs));
  app.use('/api/diff', createDiffRoutes(services.diff));
  app.use('/api/health', createHealthRoutes(services.health));
  app.use(errorHandler);
  app.listen(config.port, () => { console.log(`API server listening on http://localhost:${config.port}`); console.log(`Data directory: ${config.dataDir}`); });
}
startServer().catch((err) => { console.error('Failed to start server:', err); process.exit(1); });
