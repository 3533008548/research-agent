export interface DesktopUpdateState {
  status: "idle" | "checking" | "available" | "downloading" | "downloaded" | "installing" | "up-to-date" | "error" | "unsupported";
  currentVersion: string;
  availableVersion: string;
  releaseNotes: string;
  percent: number;
  error: string;
}
declare global {
  interface Window {
    desktop?: {
      reportError: (message: string) => void;
      info: () => Promise<{
        configured: boolean;
        dataDir: string;
        version: string;
      }>;
      configure: (value: {
        deepseekKey?: string;
        openalexKey?: string;
      }) => Promise<{ saved: boolean }>;
      chooseDataDirectory: () => Promise<{ changed: boolean }>;
      restart: () => Promise<unknown>;
      retry: () => Promise<unknown>;
      updates: {
        status: () => Promise<DesktopUpdateState>;
        check: () => Promise<DesktopUpdateState>;
        download: () => Promise<DesktopUpdateState>;
        install: () => Promise<DesktopUpdateState>;
        onStatus: (callback: (state: DesktopUpdateState) => void) => () => void;
      };
    };
  }
}
