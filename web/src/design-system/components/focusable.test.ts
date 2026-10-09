/**
 * Contract tests for `./focusable.ts`, the sequential focus navigation helpers behind
 * `Dialog`'s focus trap and `Tooltip`'s trigger check.
 *
 * Each case builds real DOM in jsdom and asserts what the module documentation promises: which
 * elements are tab stops and in what order ({@link tabbableWithin}), how one Tab or Shift+Tab
 * step moves and wraps focus ({@link moveFocusWithin}), when a focus trap may leave the step to
 * the browser ({@link leavesTabToBrowser}), and when Shift+Tab must enter a last stop with
 * internal navigation from its end ({@link shiftTabEntersLastStopFromEnd}). jsdom has no
 * `Element.checkVisibility()`, so rendering is judged by computed styles here, and one case
 * installs `checkVisibility` to hold the browser path to its contract as well.
 *
 * @packageDocumentation
 */
import {
  leavesTabToBrowser,
  moveFocusWithin,
  shiftTabEntersLastStopFromEnd,
  tabbableWithin,
} from './focusable';
import type { FocusableElement } from './focusable';

/**
 * Appends a container holding `html` to the document body.
 *
 * @param html - the container's inner HTML
 * @returns the container, which is the `root` the helpers search
 */
function mount(html: string): HTMLElement {
  const root = document.createElement('div');
  root.innerHTML = html;
  document.body.append(root);
  return root;
}

/**
 * Finds an element by id inside the document, failing the test when it is missing.
 *
 * @param id - the element's id
 * @returns the element
 */
function byId(id: string): HTMLElement {
  const element = document.getElementById(id);
  if (!(element instanceof HTMLElement)) {
    throw new Error(`No HTML element with id "${id}"`);
  }
  return element;
}

/**
 * The ids of `elements`, in order, so an order assertion reads as a list of names.
 *
 * @param elements - tab stops returned by {@link tabbableWithin}
 * @returns their ids
 */
function ids(elements: readonly FocusableElement[]): string[] {
  return elements.map((element) => element.id);
}

/** The id of the element that has focus, or `''` when none has an id. */
function focusedId(): string {
  return document.activeElement?.id ?? '';
}

afterEach(() => {
  document.body.replaceChildren();
  document.head.replaceChildren();
});

describe('tabbableWithin', () => {
  it('lists every natively focusable element, in document order, and nothing else', () => {
    const root = mount(`
      <a id="link" href="#x">link</a>
      <a id="anchor-no-href">anchor without href</a>
      <map name="m"><area id="area" href="#a" alt="area"></map>
      <img id="img" alt="" usemap="#m">
      <button id="button">button</button>
      <input id="text" type="text">
      <input id="hidden-input" type="hidden">
      <select id="select"><option>a</option></select>
      <textarea id="textarea"></textarea>
      <iframe id="iframe" title="frame"></iframe>
      <audio id="audio" controls></audio>
      <audio id="audio-no-controls"></audio>
      <video id="video" controls></video>
      <video id="video-no-controls"></video>
      <div id="editable" contenteditable="true">edit</div>
      <details id="details" open>
        <summary id="summary">toggle</summary>
        <summary id="second-summary">not the toggle</summary>
      </details>
      <div id="custom" tabindex="0">custom</div>
      <div id="plain">plain</div>
    `);

    expect(ids(tabbableWithin(root))).toEqual([
      'link',
      'area',
      'button',
      'text',
      'select',
      'textarea',
      'iframe',
      'audio',
      'video',
      'editable',
      'summary',
      'custom',
    ]);
  });

  it('never includes the root itself and returns an empty list when nothing is focusable', () => {
    const root = mount('<p>text only</p>');
    root.tabIndex = 0;

    expect(tabbableWithin(root)).toEqual([]);
  });

  it('orders positive tab indexes first, ascending and stable, then tab index 0 in document order', () => {
    const root = mount(`
      <button id="zero-a">a</button>
      <button id="three" tabindex="3">3</button>
      <button id="one-a" tabindex="1">1a</button>
      <button id="zero-b" tabindex="0">b</button>
      <button id="one-b" tabindex="1">1b</button>
      <button id="negative" tabindex="-1">out</button>
    `);

    expect(ids(tabbableWithin(root))).toEqual(['one-a', 'one-b', 'three', 'zero-a', 'zero-b']);
  });

  it('parses tabindex with HTML integer rules and ignores invalid values', () => {
    const root = mount(`
      <div id="padded" tabindex="  +2">padded and signed</div>
      <div id="trailing" tabindex="0abc">digits then junk</div>
      <div id="invalid-div" tabindex="abc">invalid on a div</div>
      <button id="invalid-button" tabindex="abc">invalid on a button</button>
      <div id="empty" tabindex="">empty</div>
      <button id="negative-padded" tabindex=" -5">negative</button>
    `);

    expect(ids(tabbableWithin(root))).toEqual(['padded', 'trailing', 'invalid-button']);
  });

  it('skips disabled controls, including those of a disabled fieldset', () => {
    const root = mount(`
      <button id="enabled">enabled</button>
      <button id="disabled" disabled>disabled</button>
      <input id="disabled-input" disabled>
      <fieldset disabled>
        <input id="in-fieldset">
        <select id="select-in-fieldset"></select>
      </fieldset>
    `);

    expect(ids(tabbableWithin(root))).toEqual(['enabled']);
  });

  it('skips inert and hidden subtrees', () => {
    const root = mount(`
      <div inert><button id="inert">inert</button></div>
      <div hidden><button id="in-hidden">hidden</button></div>
      <button id="hidden-itself" hidden>hidden itself</button>
      <button id="visible">visible</button>
    `);

    expect(ids(tabbableWithin(root))).toEqual(['visible']);
  });

  it('skips the content of a closed details element but keeps its toggle and what the toggle holds', () => {
    const root = mount(`
      <details>
        <summary id="toggle">toggle <a id="link-in-toggle" href="#t">in toggle</a></summary>
        <button id="content">content</button>
        <div><input id="deep-content"></div>
      </details>
      <details><div><button id="no-summary">no summary</button></div></details>
      <details open>
        <summary id="open-toggle">open</summary>
        <button id="open-content">open content</button>
      </details>
    `);

    expect(ids(tabbableWithin(root))).toEqual([
      'toggle',
      'link-in-toggle',
      'open-toggle',
      'open-content',
    ]);
  });

  it('skips elements that are not rendered: display none on them or an ancestor', () => {
    const root = mount(`
      <button id="none" style="display: none">none</button>
      <div style="display: none"><div><button id="in-none">in none</button></div></div>
      <button id="shown">shown</button>
    `);

    expect(ids(tabbableWithin(root))).toEqual(['shown']);
  });

  it('skips invisible elements, own or inherited visibility, and honours a visible override', () => {
    const root = mount(`
      <button id="own-hidden" style="visibility: hidden">hidden</button>
      <button id="own-collapse" style="visibility: collapse">collapse</button>
      <div style="visibility: hidden">
        <button id="inherited-hidden">inherited</button>
        <button id="override" style="visibility: visible">override</button>
      </div>
    `);

    expect(ids(tabbableWithin(root))).toEqual(['override']);
  });

  it('applies display none from a stylesheet rule', () => {
    const style = document.createElement('style');
    style.textContent = '.collapsed { display: none; }';
    document.head.append(style);
    const root = mount(`
      <div class="collapsed"><button id="collapsed">collapsed</button></div>
      <button id="open">open</button>
    `);

    expect(ids(tabbableWithin(root))).toEqual(['open']);
  });

  it('skips content under content-visibility hidden, but not the element that sets it', () => {
    const root = mount(`
      <div style="content-visibility: hidden"><button id="skipped">skipped</button></div>
      <button id="own" style="content-visibility: hidden">own</button>
    `);

    expect(ids(tabbableWithin(root))).toEqual(['own']);
  });

  it('judges rendering inside a shadow tree through the shadow host', () => {
    const shownHost = mount('');
    const shownShadow = shownHost.attachShadow({ mode: 'open' });
    shownShadow.innerHTML = '<div id="shown-root"><button id="shown-button">b</button></div>';
    const hiddenHost = mount('');
    hiddenHost.style.display = 'none';
    const hiddenShadow = hiddenHost.attachShadow({ mode: 'open' });
    hiddenShadow.innerHTML = '<div id="hidden-root"><button id="hidden-button">b</button></div>';

    const shownRoot = shownShadow.getElementById('shown-root');
    const hiddenRoot = hiddenShadow.getElementById('hidden-root');
    if (shownRoot === null || hiddenRoot === null) {
      throw new Error('shadow roots not built');
    }

    expect(ids(tabbableWithin(shownRoot))).toEqual(['shown-button']);
    expect(tabbableWithin(hiddenRoot)).toEqual([]);
  });

  it('uses Element.checkVisibility where the engine has it, with the CSS and visibility checks on', () => {
    const root = mount(`
      <button id="kept">kept</button>
      <button id="dropped" style="display: none">dropped by styles, kept by the engine</button>
      <button id="refused">refused by the engine</button>
    `);
    const checkVisibility = vi.fn(function (this: Element): boolean {
      return this.id !== 'refused';
    });
    Object.defineProperty(Element.prototype, 'checkVisibility', {
      configurable: true,
      writable: true,
      value: checkVisibility,
    });
    try {
      expect(ids(tabbableWithin(root))).toEqual(['kept', 'dropped']);
      expect(checkVisibility).toHaveBeenCalledWith({
        checkVisibilityCSS: true,
        visibilityProperty: true,
      });
    } finally {
      Reflect.deleteProperty(Element.prototype, 'checkVisibility');
    }
    expect(typeof Element.prototype.checkVisibility).toBe('undefined');
  });

  it('treats only the outermost editing host as a stop', () => {
    const root = mount(`
      <div id="outer" contenteditable="">
        outer
        <div id="nested" contenteditable="true">nested host inside editable content</div>
        <div id="island" contenteditable="false">
          <span id="host-in-island" contenteditable="plaintext-only">host inside a non-editable island</span>
        </div>
      </div>
      <div id="upper-case" contenteditable="TRUE">upper case value</div>
      <div id="invalid" contenteditable="bogus">invalid value is the inherit state</div>
      <div id="false" contenteditable="false">false</div>
    `);

    expect(ids(tabbableWithin(root))).toEqual(['outer', 'host-in-island', 'upper-case']);
  });

  it('includes SVG elements with a tab index but never takes contenteditable from non-HTML elements', () => {
    const root = mount(`
      <svg id="svg-stop" tabindex="0"><a id="svg-link" href="#s"><text>svg link</text></a></svg>
      <svg id="svg-editable" contenteditable="true"></svg>
    `);

    // The SVG `a` matches `a[href]` and is natively focusable by tag name.
    expect(ids(tabbableWithin(root))).toEqual(['svg-stop', 'svg-link']);
  });

  it('ignores candidates that are neither HTML nor SVG elements', () => {
    const root = mount('<button id="button">button</button>');
    const math = document.createElementNS('http://www.w3.org/1998/Math/MathML', 'math');
    math.setAttribute('tabindex', '0');
    root.append(math);

    expect(ids(tabbableWithin(root))).toEqual(['button']);
  });

  it('keeps one stop per named radio group: the checked radio, or else the first enabled one', () => {
    const root = mount(`
      <input id="a1" type="radio" name="a">
      <input id="a2" type="radio" name="a" checked>
      <input id="b1" type="radio" name="b" disabled>
      <input id="b2" type="radio" name="b">
      <input id="b3" type="radio" name="b">
      <input id="unnamed-1" type="radio">
      <input id="unnamed-2" type="radio">
      <form><input id="f1-a" type="radio" name="a"></form>
      <form><input id="f2-a" type="radio" name="a"></form>
    `);

    expect(ids(tabbableWithin(root))).toEqual([
      'a2',
      'b2',
      'unnamed-1',
      'unnamed-2',
      'f1-a',
      'f2-a',
    ]);
  });

  it('includes an area only when a rendered image uses its map, by name or by id', () => {
    const root = mount(`
      <map name="by-name"><area id="area-by-name" href="#1" alt="1"></map>
      <img alt="" usemap="#by-name">
      <map id="by-id"><area id="area-by-id" href="#2" alt="2"></map>
      <img alt="" usemap="#by-id">
      <map name="unused"><area id="area-unused" href="#3" alt="3"></map>
      <map name="no-hash"><area id="area-no-hash" href="#4" alt="4"></map>
      <img alt="" usemap="no-hash">
      <map name="hidden-image"><area id="area-hidden-image" href="#5" alt="5"></map>
      <img alt="" usemap="#hidden-image" style="display: none">
      <div inert>
        <map name="inert-map"><area id="area-inert" href="#6" alt="6"></map>
      </div>
      <img alt="" usemap="#inert-map">
      <map name="no-href"><area id="area-no-href" alt="7"></map>
      <img alt="" usemap="#no-href">
    `);
    const orphan = document.createElement('area');
    orphan.id = 'area-orphan';
    orphan.href = '#8';
    root.append(orphan);

    expect(ids(tabbableWithin(root))).toEqual(['area-by-name', 'area-by-id']);
  });
});

describe('moveFocusWithin', () => {
  it('returns false and leaves focus alone when the root has no tab stop', () => {
    const outside = mount('<button id="outside">outside</button>');
    const root = mount('<p>nothing to focus</p>');
    byId('outside').focus();

    expect(moveFocusWithin(root, false)).toBe(false);
    expect(moveFocusWithin(root, true)).toBe(false);
    expect(document.activeElement).toBe(outside.firstElementChild);
  });

  it('enters at the first stop with Tab and at the last with Shift+Tab, from outside the root', () => {
    mount('<button id="outside">outside</button>');
    const root = mount('<button id="first">1</button><button id="last">2</button>');

    byId('outside').focus();
    expect(moveFocusWithin(root, false)).toBe(true);
    expect(focusedId()).toBe('first');

    byId('outside').focus();
    expect(moveFocusWithin(root, true)).toBe(true);
    expect(focusedId()).toBe('last');
  });

  it('enters at the matching end from the root itself and when nothing has focus', () => {
    const root = mount('<button id="first">1</button><button id="last">2</button>');
    root.tabIndex = -1;

    root.focus();
    moveFocusWithin(root, false);
    expect(focusedId()).toBe('first');

    root.focus();
    moveFocusWithin(root, true);
    expect(focusedId()).toBe('last');

    (document.activeElement as HTMLElement).blur();
    moveFocusWithin(root, false);
    expect(focusedId()).toBe('first');
  });

  it('steps forward and backward between stops and wraps at either end', () => {
    const root = mount(
      '<button id="a">a</button><button id="b">b</button><button id="c">c</button>',
    );

    byId('a').focus();
    moveFocusWithin(root, false);
    expect(focusedId()).toBe('b');
    moveFocusWithin(root, false);
    expect(focusedId()).toBe('c');
    moveFocusWithin(root, false);
    expect(focusedId()).toBe('a');

    moveFocusWithin(root, true);
    expect(focusedId()).toBe('c');
    moveFocusWithin(root, true);
    expect(focusedId()).toBe('b');
  });

  it('follows the positive tab index order', () => {
    const root = mount(`
      <button id="zero">zero</button>
      <button id="two" tabindex="2">two</button>
      <button id="one" tabindex="1">one</button>
    `);

    byId('one').focus();
    moveFocusWithin(root, false);
    expect(focusedId()).toBe('two');
    moveFocusWithin(root, false);
    expect(focusedId()).toBe('zero');
    moveFocusWithin(root, false);
    expect(focusedId()).toBe('one');
  });

  it('starts from a focused non-stop at its place in document order', () => {
    const root = mount(`
      <button id="a">a</button>
      <div id="between" tabindex="-1">between</div>
      <button id="b">b</button>
      <div id="after" tabindex="-1">after</div>
    `);

    byId('between').focus();
    moveFocusWithin(root, false);
    expect(focusedId()).toBe('b');

    byId('between').focus();
    moveFocusWithin(root, true);
    expect(focusedId()).toBe('a');

    byId('after').focus();
    moveFocusWithin(root, false);
    expect(focusedId()).toBe('a');

    byId('after').focus();
    moveFocusWithin(root, true);
    expect(focusedId()).toBe('b');
  });

  it('starts from a radio that is not its group stop at its place in document order', () => {
    const root = mount(`
      <button id="before">before</button>
      <input id="r1" type="radio" name="g" checked>
      <input id="r2" type="radio" name="g">
      <button id="after">after</button>
    `);

    byId('r2').focus();
    moveFocusWithin(root, false);
    expect(focusedId()).toBe('after');
  });

  it('skips a stop that refuses focus and reports false when no stop accepts it', () => {
    // jsdom cannot focus `audio` without a tabindex attribute, as some engines cannot.
    const root = mount('<button id="a">a</button><audio controls></audio><button id="b">b</button>');
    byId('a').focus();

    expect(moveFocusWithin(root, false)).toBe(true);
    expect(focusedId()).toBe('b');

    mount('<button id="outside">outside</button>');
    const refusing = mount('<audio controls></audio>');
    byId('outside').focus();

    expect(moveFocusWithin(refusing, false)).toBe(false);
    expect(focusedId()).toBe('outside');
  });
});

describe('leavesTabToBrowser', () => {
  it('returns false when focus is outside the root, on the root, or nowhere', () => {
    mount('<button id="outside">outside</button>');
    const root = mount('<button id="a">a</button><button id="b">b</button>');
    root.tabIndex = -1;

    byId('outside').focus();
    expect(leavesTabToBrowser(root, false)).toBe(false);

    root.focus();
    expect(leavesTabToBrowser(root, false)).toBe(false);

    (document.activeElement as HTMLElement).blur();
    expect(leavesTabToBrowser(root, true)).toBe(false);
  });

  it('returns false when the root has no stop, even with focus inside it', () => {
    const root = mount('<div id="non-stop" tabindex="-1">focusable by script only</div>');
    byId('non-stop').focus();

    expect(leavesTabToBrowser(root, false)).toBe(false);
  });

  it('keeps the browser step between two tab index 0 stops and takes over at the ends', () => {
    const root = mount(
      '<button id="a">a</button><button id="b">b</button><button id="c">c</button>',
    );

    byId('b').focus();
    expect(leavesTabToBrowser(root, false)).toBe(true);
    expect(leavesTabToBrowser(root, true)).toBe(true);

    byId('c').focus();
    expect(leavesTabToBrowser(root, false)).toBe(false);
    expect(leavesTabToBrowser(root, true)).toBe(true);

    byId('a').focus();
    expect(leavesTabToBrowser(root, true)).toBe(false);
    expect(leavesTabToBrowser(root, false)).toBe(true);
  });

  it('takes over every step to or from a positive tab index', () => {
    const root = mount(`
      <button id="positive" tabindex="1">positive</button>
      <button id="zero-a">a</button>
      <button id="zero-b">b</button>
    `);

    byId('positive').focus();
    expect(leavesTabToBrowser(root, false)).toBe(false);

    byId('zero-a').focus();
    expect(leavesTabToBrowser(root, true)).toBe(false);
    expect(leavesTabToBrowser(root, false)).toBe(true);
  });

  it('keeps the browser step from a focused non-stop between tab index 0 stops', () => {
    const root = mount(`
      <button id="a">a</button>
      <div id="between" tabindex="-1">between</div>
      <button id="b">b</button>
    `);
    byId('between').focus();

    expect(leavesTabToBrowser(root, false)).toBe(true);
    expect(leavesTabToBrowser(root, true)).toBe(true);
  });

  it('keeps the browser step on a stop with internal navigation, even at either end', () => {
    const root = mount(`
      <iframe id="iframe" title="frame"></iframe>
      <button id="middle">middle</button>
      <video id="video" controls tabindex="0"></video>
      <audio id="audio" controls tabindex="0"></audio>
      <input id="date" type="date">
      <input id="datetime" type="datetime-local">
      <input id="month" type="month">
      <input id="time" type="time">
      <input id="week" type="week">
    `);

    byId('iframe').focus();
    expect(leavesTabToBrowser(root, true)).toBe(true);

    for (const id of ['video', 'audio', 'date', 'datetime', 'month', 'time']) {
      byId(id).focus();
      expect(focusedId()).toBe(id);
      expect(leavesTabToBrowser(root, false)).toBe(true);
    }

    // The last stop: Tab would wrap, yet its inner fields come first.
    byId('week').focus();
    expect(focusedId()).toBe('week');
    expect(leavesTabToBrowser(root, false)).toBe(true);
  });

  it('does not treat a video without controls or a text input as having internal navigation', () => {
    const root = mount(`
      <button id="a">a</button>
      <video id="video" tabindex="0"></video>
      <input id="text" type="text">
    `);

    byId('video').focus();
    expect(leavesTabToBrowser(root, false)).toBe(true);

    byId('text').focus();
    expect(leavesTabToBrowser(root, false)).toBe(false);
  });
});

describe('shiftTabEntersLastStopFromEnd', () => {
  it('returns false when the root has no stop', () => {
    const root = mount('<p>nothing</p>');

    expect(shiftTabEntersLastStopFromEnd(root)).toBe(false);
  });

  it('returns false when the last stop has no internal navigation', () => {
    mount('<button id="outside">outside</button>');
    const root = mount('<input id="date" type="date"><button id="last">last</button>');
    byId('outside').focus();

    expect(shiftTabEntersLastStopFromEnd(root)).toBe(false);
  });

  it('returns false when the only stops have a positive tab index', () => {
    mount('<button id="outside">outside</button>');
    const root = mount('<input id="date" type="date" tabindex="1">');
    byId('outside').focus();

    expect(shiftTabEntersLastStopFromEnd(root)).toBe(false);
  });

  it('returns true when Shift+Tab wraps or enters onto a last stop with internal navigation', () => {
    mount('<button id="outside">outside</button>');
    const root = mount(`
      <button id="first">first</button>
      <button id="middle">middle</button>
      <iframe id="frame" title="frame"></iframe>
    `);
    root.tabIndex = -1;

    byId('outside').focus();
    expect(shiftTabEntersLastStopFromEnd(root)).toBe(true);

    root.focus();
    expect(shiftTabEntersLastStopFromEnd(root)).toBe(true);

    byId('first').focus();
    expect(shiftTabEntersLastStopFromEnd(root)).toBe(true);

    byId('middle').focus();
    expect(shiftTabEntersLastStopFromEnd(root)).toBe(false);

    byId('frame').focus();
    expect(shiftTabEntersLastStopFromEnd(root)).toBe(false);
  });

  it('returns true from a focused non-stop after every tab index 0 stop', () => {
    const root = mount(`
      <button id="first">first</button>
      <input id="date" type="date">
      <div id="after" tabindex="-1">after</div>
    `);
    byId('after').focus();

    expect(shiftTabEntersLastStopFromEnd(root)).toBe(true);
  });
});
