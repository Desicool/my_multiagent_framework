import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { NotificationService } from './notifications';

describe('NotificationService title flash', () => {
  let svc: NotificationService;

  beforeEach(() => {
    document.title = 'Beidou';
    svc = new NotificationService();
    svc.init();
  });

  afterEach(() => {
    svc.destroy();
  });

  it('does not flash when count is 0', () => {
    svc.setQuestionCount(0);
    expect(document.title).toBe('Beidou');
  });

  it('flashes when count > 0 and tab is hidden', () => {
    // jsdom: visibilityState is 'visible' by default, hasFocus → true.
    // Force the "hidden" branch by stubbing document.hasFocus to return false:
    Object.defineProperty(document, 'hasFocus', { value: () => false, configurable: true });
    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true });
    svc.setQuestionCount(2);
    // After one second the title should flip. Easier: just check that flashing is enabled
    // by watching that the title eventually contains "(2)".
    return new Promise<void>((resolve) => {
      setTimeout(() => {
        expect(document.title).toMatch(/\(2\)/);
        resolve();
      }, 1100);
    });
  }, 3000);

  it('clears flash when count goes back to 0', () => {
    Object.defineProperty(document, 'hasFocus', { value: () => false, configurable: true });
    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true });
    svc.setQuestionCount(3);
    svc.setQuestionCount(0);
    expect(document.title).toBe('Beidou');
  });

  it('getPermissionStatus returns Notification.permission default', () => {
    // jsdom may not implement Notification; tolerate both.
    const status = svc.getPermissionStatus();
    expect(['default', 'granted', 'denied']).toContain(status);
  });
});
