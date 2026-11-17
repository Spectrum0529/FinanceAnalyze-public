import { Component } from 'react';
import type { ReactNode } from 'react';
import { ApiError } from './api';
import { ErrorBox } from './components';

export class PageBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    if (this.state.failed) {
      return (
        <div>
          <ErrorBox
            error={
              new ApiError(0, 'PAGE_LOAD_FAILED', '暂时打不开这页。请刷新页面，或从菜单打开其他页面。')
            }
          />
          <button type="button" className="secondary" onClick={() => window.location.reload()}>
            刷新页面
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
