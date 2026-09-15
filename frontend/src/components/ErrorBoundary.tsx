import { Component, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
}

export default class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo): void {
    console.error("ErrorBoundary caught:", error, info.componentStack);
  }

  render() {
    if (this.state.hasError) {
      return (
        // On the surface rather than the browser's defaults: an unstyled
        // fallback is white-on-white in the light theme and black-on-white in
        // the middle of the dark one.
        <div className="crash-screen">
          <h2>Something went wrong</h2>
          <p>Please refresh the page. If the problem persists, contact support.</p>
          <button
            className="btn btn-secondary"
            onClick={() => this.setState({ hasError: false })}
          >
            Try again
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
