export function matchesSearch(text: string, query: string): boolean {
  return text.toLowerCase().includes(query.toLowerCase());
}
export function toggleArrayItem<T>(arr: T[], item: T): T[] {
  const index = arr.indexOf(item);
  if (index >= 0) return [...arr.slice(0, index), ...arr.slice(index + 1)];
  return [...arr, item];
}
