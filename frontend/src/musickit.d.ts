interface MusicKitConfiguration {
  developerToken: string;
  app: {
    name: string;
    build: string;
  };
}

interface MusicKitInstance {
  authorize(): Promise<string>;
  unauthorize(): Promise<void>;
}

interface MusicKitGlobal {
  configure(configuration: MusicKitConfiguration): Promise<void>;
  getInstance(): MusicKitInstance;
}

declare global {
  interface Window {
    MusicKit?: MusicKitGlobal;
  }

  interface DocumentEventMap {
    musickitloaded: Event;
  }
}

export {};
