/**
 * @file logger.ts
 * @description Structured pino logger for service layer.
 */

import pino from 'pino';

export const logger = pino({
  name: 'ma-services',
  level: process.env.LOG_LEVEL || 'info',
  transport: process.env.NODE_ENV !== 'production'
    ? { target: 'pino-pretty', options: { colorize: true } }
    : undefined,
});

export type Logger = pino.Logger;
