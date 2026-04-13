export function toCSV<T extends Record<string, unknown>>(data: T[], columns: Array<{ key: keyof T; header: string }>): string {
  const header = columns.map(c => `"${String(c.header)}"`).join(',');
  const rows = data.map(item => columns.map(c => { const val = item[c.key]; if (val == null) return ''; if (typeof val === 'string') return `"${val.replace(/"/g, '""')}"`; return String(val); }).join(','));
  return [header, ...rows].join('\n');
}
export function downloadCSV(csvContent: string, filename: string): void {
  const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a'); link.href = url; link.download = filename; link.click();
  URL.revokeObjectURL(url);
}
