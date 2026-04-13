type LogLevel = 'debug' | 'info' | 'warn' | 'error';
const LEVELS: Record<LogLevel, number> = { debug: 0, info: 1, warn: 2, error: 3 };
const minLevel = LEVELS['info'];
function shouldLog(level: LogLevel): boolean { return LEVELS[level] >= minLevel; }
export const log = {
  debug(msg: string, data?: Record<string, unknown>) { if (shouldLog('debug')) console.debug(`[DEBUG] ${msg}`, data || ''); },
  info(msg: string, data?: Record<string, unknown>) { if (shouldLog('info')) console.info(`[INFO] ${msg}`, data || ''); },
  warn(msg: string, data?: Record<string, unknown>) { if (shouldLog('warn')) console.warn(`[WARN] ${msg}`, data || ''); },
  error(msg: string, data?: Record<string, unknown>) { if (shouldLog('error')) console.error(`[ERROR] ${msg}`, data || ''); },
};
