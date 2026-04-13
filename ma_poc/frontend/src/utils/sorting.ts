export function createComparator<T>(field: keyof T, direction: 'asc' | 'desc'): (a: T, b: T) => number {
  const mult = direction === 'asc' ? 1 : -1;
  return (a: T, b: T) => {
    const aVal = a[field]; const bVal = b[field];
    if (aVal == null && bVal == null) return 0;
    if (aVal == null) return 1; if (bVal == null) return -1;
    if (typeof aVal === 'string' && typeof bVal === 'string') return mult * aVal.localeCompare(bVal);
    return mult * ((aVal as number) - (bVal as number));
  };
}
