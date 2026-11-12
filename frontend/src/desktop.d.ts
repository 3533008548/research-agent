export {};
declare global {
  interface Window {
    desktop?: {
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
    };
  }
}
