import type { PendingQuestion } from './types';

export class NotificationService {
  private titleInterval: number | null = null;
  private originalTitle = 'Beidou';
  private currentCount = 0;
  private faviconLink: HTMLLinkElement | null = null;
  private baseFaviconUrl = '/favicon.png';
  private offCanvas: HTMLCanvasElement | null = null;
  private permission: NotificationPermission = 'default';
  private visibilityHandler: (() => void) | null = null;
  private focusHandler: (() => void) | null = null;
  private blurHandler: (() => void) | null = null;
  private initialized = false;

  init(): void {
    if (this.initialized) return;
    this.initialized = true;
    if (typeof document === 'undefined') return;
    this.originalTitle = document.title || 'Beidou';
    this.faviconLink = document.querySelector('link[rel="icon"]');
    this.permission =
      typeof Notification !== 'undefined' ? Notification.permission : 'denied';

    // Pause flashing when tab focused; resume on blur if questions pending.
    this.visibilityHandler = () => this.applyState();
    this.focusHandler = () => this.applyState();
    this.blurHandler = () => this.applyState();
    document.addEventListener('visibilitychange', this.visibilityHandler);
    window.addEventListener('focus', this.focusHandler);
    window.addEventListener('blur', this.blurHandler);
  }

  destroy(): void {
    if (!this.initialized) return;
    if (this.visibilityHandler) document.removeEventListener('visibilitychange', this.visibilityHandler);
    if (this.focusHandler) window.removeEventListener('focus', this.focusHandler);
    if (this.blurHandler) window.removeEventListener('blur', this.blurHandler);
    this.stopTitleFlash();
    this.initialized = false;
  }

  setQuestionCount(n: number): void {
    this.currentCount = Math.max(0, n | 0);
    this.applyState();
  }

  /** Recompute title flash + favicon based on count + visibility/focus. */
  private applyState(): void {
    if (!this.initialized) return;
    const tabFocused =
      typeof document !== 'undefined' &&
      document.visibilityState === 'visible' &&
      typeof document.hasFocus === 'function' &&
      document.hasFocus();

    if (this.currentCount === 0 || tabFocused) {
      this.stopTitleFlash();
      this.setFaviconDot(false);
      document.title = this.originalTitle;
    } else {
      this.startTitleFlash();
      this.setFaviconDot(true);
    }
  }

  private startTitleFlash(): void {
    if (this.titleInterval !== null) return;
    let alt = false;
    this.titleInterval = window.setInterval(() => {
      alt = !alt;
      document.title = alt
        ? `• (${this.currentCount}) Question — ${this.originalTitle}`
        : this.originalTitle;
    }, 1000);
  }

  private stopTitleFlash(): void {
    if (this.titleInterval !== null) {
      clearInterval(this.titleInterval);
      this.titleInterval = null;
    }
    document.title = this.originalTitle;
  }

  /** Draw a red dot overlay onto the favicon if active. */
  private async setFaviconDot(active: boolean): Promise<void> {
    if (!this.faviconLink) return;
    if (!active) {
      this.faviconLink.href = this.baseFaviconUrl;
      return;
    }
    try {
      if (!this.offCanvas) {
        const c = document.createElement('canvas');
        c.width = 32;
        c.height = 32;
        this.offCanvas = c;
      }
      const c = this.offCanvas;
      const ctx = c.getContext('2d');
      if (!ctx) return;
      ctx.clearRect(0, 0, 32, 32);
      const img = new Image();
      img.crossOrigin = 'anonymous';
      img.src = this.baseFaviconUrl;
      await new Promise<void>((resolve) => {
        img.onload = () => resolve();
        img.onerror = () => resolve();
      });
      try { ctx.drawImage(img, 0, 0, 32, 32); } catch { /* tainted; skip */ }
      ctx.fillStyle = '#dc2626';
      ctx.beginPath();
      ctx.arc(24, 8, 7, 0, Math.PI * 2);
      ctx.fill();
      ctx.lineWidth = 2;
      ctx.strokeStyle = '#fff';
      ctx.stroke();
      this.faviconLink.href = c.toDataURL('image/png');
    } catch {
      /* ignore favicon errors */
    }
  }

  async requestPermission(): Promise<NotificationPermission> {
    if (typeof Notification === 'undefined') return 'denied';
    if (Notification.permission !== 'default') {
      this.permission = Notification.permission;
      return this.permission;
    }
    const result = await Notification.requestPermission();
    this.permission = result;
    return result;
  }

  getPermissionStatus(): NotificationPermission {
    return this.permission;
  }

  notifyNewQuestion(q: PendingQuestion, askerLabel: string): void {
    if (typeof Notification === 'undefined') return;
    if (Notification.permission !== 'granted') return;
    if (typeof document !== 'undefined' && document.visibilityState === 'visible' && document.hasFocus?.()) return;
    try {
      const n = new Notification(`Beidou: ${askerLabel} is asking`, {
        body: (q.prompt || '').slice(0, 140),
        tag: q.qid,
      });
      n.onclick = () => {
        try { window.focus(); } catch { /* noop */ }
        const ev = new CustomEvent('beidou:pin-agent', { detail: { agentId: q.asker_agent_id } });
        window.dispatchEvent(ev);
        n.close();
      };
    } catch {
      /* ignore */
    }
  }
}

export const notifications = new NotificationService();
