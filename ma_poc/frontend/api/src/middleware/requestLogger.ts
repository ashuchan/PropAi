import pinoHttp from 'pino-http';
import pino from 'pino';
export function createRequestLogger(level: string) {
  return pinoHttp({ logger: pino({ level, transport: process.env.NODE_ENV !== 'production' ? { target: 'pino-pretty', options: { colorize: true } } : undefined }) });
}
