import { Component, type ReactNode } from 'react';
import { AlertTriangle, RefreshCw } from 'lucide-react';
interface Props { children: ReactNode; fallback?: ReactNode; }
interface State { hasError: boolean; error: Error | null; }
export class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) { super(props); this.state = { hasError: false, error: null }; }
  static getDerivedStateFromError(error: Error): State { return { hasError: true, error }; }
  handleRetry = () => { this.setState({ hasError: false, error: null }); };
  render() {
    if (this.state.hasError) {
      if (this.props.fallback) return this.props.fallback;
      return (
        <div className="flex flex-col items-center justify-center py-16 text-center" data-testid="error-state">
          <AlertTriangle size={48} className="mb-4 text-red-400" />
          <h3 className="text-[16px] font-medium text-slate-900 dark:text-slate-100">Something went wrong</h3>
          <p className="mt-1 max-w-md text-[13px] text-slate-500 dark:text-slate-400">{this.state.error?.message || 'An unexpected error occurred'}</p>
          <button onClick={this.handleRetry} className="mt-4 inline-flex items-center gap-2 rounded-lg bg-slate-100 px-4 py-2 text-[13px] font-medium text-slate-700 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700 transition-colors"><RefreshCw size={14} />Try again</button>
        </div>
      );
    }
    return this.props.children;
  }
}
