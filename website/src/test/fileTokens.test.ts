import { describe, it, expect } from 'vitest'
import { prepareSendPayload, buildFileLabels, resolveFileSegment, mdImageDest, mdImageDestToPath, restoreQueuedContent, serializeDirTokens } from '../utils/fileTokens'

describe('buildFileLabels uniqueness', () => {
  it('disambiguates paths that share a basename', () => {
    const m = buildFileLabels(['/q3/report.docx', '/q4/report.docx'])
    expect(m.get('/q3/report.docx')).toBe('q3/report.docx')
    expect(m.get('/q4/report.docx')).toBe('q4/report.docx')
  })

  it('widens past two segments when the last two also collide', () => {
    // Regression: both collapsed to `x/report.docx`, so two distinct
    // attachments got the same chip label and the same mentionMap key.
    const m = buildFileLabels(['/a/x/report.docx', '/b/x/report.docx'])
    expect(new Set([...m.values()]).size, 'labels must be distinct').toBe(2)
    expect(m.get('/a/x/report.docx')).not.toBe(m.get('/b/x/report.docx'))
  })

  it('leaves an already-unique basename alone', () => {
    const m = buildFileLabels(['/repo/notes.txt', '/repo/other.txt'])
    expect(m.get('/repo/notes.txt')).toBe('notes.txt')
    expect(m.get('/repo/other.txt')).toBe('other.txt')
  })

  it('keeps a colliding mention resolvable to its own path', () => {
    // The label is also the mentionMap key, so a collision made one path
    // unreachable from its own chip.
    const content = '[attached_file 1] /a/x/report.docx\n[attached_file 2] /b/x/report.docx'
    const r = resolveFileSegment(content, ['/a/x/report.docx', '/b/x/report.docx'])
    const targets = new Set([...r.mentionMap.values(), ...r.cardPaths])
    expect(targets.has('/a/x/report.docx')).toBe(true)
    expect(targets.has('/b/x/report.docx')).toBe(true)
  })
})

describe('prepareSendPayload', () => {
  it('includes non-image files without @-mention', () => {
    const result = prepareSendPayload('hello', ['/tmp/data.csv'])
    expect(result.txt).toContain('[attached_file 1]')
    expect(result.txt).toContain('/tmp/data.csv')
    expect(result.filePaths).toEqual(['/tmp/data.csv'])
  })

  it('includes image files as markdown', () => {
    const result = prepareSendPayload('check this', ['/tmp/photo.png'])
    expect(result.txt).toContain('![image](/tmp/photo.png)')
    expect(result.imgPaths).toEqual(['/tmp/photo.png'])
  })

  it('separates the image from the message text with a blank line in displayTxt', () => {
    const result = prepareSendPayload('my caption', ['/tmp/photo.png'])
    // Blank line (Markdown paragraph break) so the image renders in its own
    // block and the text drops to the next line, not inline after the image.
    expect(result.displayTxt).toBe('![image](/tmp/photo.png)\n\nmy caption')
    expect(result.displayTxt).toContain('![image](/tmp/photo.png)\n\nmy caption')
  })

  it('emits image-only displayTxt with no trailing separator when text is empty', () => {
    const result = prepareSendPayload('', ['/tmp/photo.png'])
    expect(result.displayTxt).toBe('![image](/tmp/photo.png)')
  })

  it('separates the image from the message text with a blank line in txt (LLM-facing)', () => {
    const result = prepareSendPayload('my caption', ['/tmp/photo.png'])
    // The blank-line separation is persisted in the LLM-facing `txt`, not just
    // the optimistic displayTxt, so the image renders in its own block on every
    // surface that replays stored content (dashboard re-render after a turn,
    // gateway restart, Slack replay, exports) — the original bug was the
    // single-'\n' persisted content collapsing image + caption onto one line.
    expect(result.txt).toBe('![image](/tmp/photo.png)\n\nmy caption')
  })

  it('emits image-only txt with no trailing separator when text is empty', () => {
    const result = prepareSendPayload('', ['/tmp/photo.png'])
    expect(result.txt).toBe('![image](/tmp/photo.png)')
  })

  it('separates image from caption with a blank line but keeps single newline to appended file tokens', () => {
    const result = prepareSendPayload('my caption', ['/tmp/photo.png', '/tmp/data.csv'])
    // image block -> blank line -> caption -> single newline -> [attached_file].
    expect(result.txt).toBe(
      '![image](/tmp/photo.png)\n\nmy caption\n[attached_file 1] /tmp/data.csv',
    )
  })

  it('includes mixed image and non-image files', () => {
    const result = prepareSendPayload('here', ['/tmp/a.png', '/tmp/b.zip'])
    expect(result.imgPaths).toEqual(['/tmp/a.png'])
    expect(result.filePaths).toEqual(['/tmp/b.zip'])
    expect(result.txt).toContain('![image]')
    expect(result.txt).toContain('[attached_file')
    expect(result.displayTxt).not.toContain('[attached_file')
    expect(result.displayTxt).toContain('![image]')
  })

  // Issue #3497: on Windows the upload endpoint returns backslash paths
  // (`C:\Users\me\.kiro\crew\uploads\x.png`). In a markdown destination
  // CommonMark eats `\` before punctuation (`\.` -> `.`), mangling the path,
  // and the drive letter parses as an unknown `c:` scheme that the URL
  // sanitizer empties — so the sender's bubble rendered no image at all.
  describe('windows image paths (issue #3497)', () => {
    it('emits a drive path in forward-slash form in both txt and displayTxt', () => {
      const result = prepareSendPayload('caption', ['C:\\Users\\me\\.kiro\\crew\\uploads\\shot.png'])
      expect(result.txt).toContain('![image](C:/Users/me/.kiro/crew/uploads/shot.png)')
      expect(result.displayTxt).toContain('![image](C:/Users/me/.kiro/crew/uploads/shot.png)')
      // imgPaths keeps the original path — it is the server-side identity.
      expect(result.imgPaths).toEqual(['C:\\Users\\me\\.kiro\\crew\\uploads\\shot.png'])
    })

    it('wraps a destination containing spaces in angle brackets', () => {
      const result = prepareSendPayload('', ['C:\\Users\\John Doe\\uploads\\shot.png'])
      expect(result.displayTxt).toBe('![image](<C:/Users/John Doe/uploads/shot.png>)')
    })

    it('wraps a POSIX destination containing spaces without touching separators', () => {
      const result = prepareSendPayload('', ['/tmp/my shots/pic.png'])
      expect(result.displayTxt).toBe('![image](</tmp/my shots/pic.png>)')
    })

    it('normalizes a UNC share path to forward slashes (roaming profiles)', () => {
      const result = prepareSendPayload('', ['\\\\fileserver\\home\\me\\.kiro\\crew\\uploads\\shot.png'])
      expect(result.displayTxt).toBe('![image](//fileserver/home/me/.kiro/crew/uploads/shot.png)')
    })

    it('wraps and escapes a literal % so consumers decode only marked forms', () => {
      // The wrap is the provenance marker: without it, a legacy destination
      // containing `%20` would be indistinguishable from producer-encoded
      // output and a file literally named `photo%20copy.png` would decode to
      // `photo copy.png` and fetch the wrong file.
      const result = prepareSendPayload('', ['/tmp/photo%20copy.png'])
      expect(result.displayTxt).toBe('![image](</tmp/photo%2520copy.png>)')
    })

    it('escapes backslash-before-punctuation via the bracketed form (POSIX)', () => {
      // `\.` in a plain destination is a CommonMark escape that collapses to
      // `.` — the wrap + escape keeps the on-disk name intact.
      const result = prepareSendPayload('', ['/tmp/my dir\\.hidden.png'])
      expect(result.displayTxt).toBe('![image](</tmp/my dir\\\\.hidden.png>)')
    })

    it('escapes angle brackets inside the bracketed form', () => {
      const result = prepareSendPayload('', ['/tmp/a <b>.png'])
      expect(result.displayTxt).toBe('![image](</tmp/a \\<b\\>.png>)')
    })

    it('wraps a POSIX path containing a backslash (letter-follow case included)', () => {
      // Any backslash routes into the escaped bracketed form — `\n` after `\`
      // is not a CommonMark escape, but wrapping uniformly keeps one rule.
      const result = prepareSendPayload('', ['/tmp/weird\\name.png'])
      expect(result.displayTxt).toBe('![image](</tmp/weird\\\\name.png>)')
    })

    it('wraps a parenthesized duplicate-name path without escaping the parens', () => {
      // Parens are legal inside CommonMark's <…> destination.
      const result = prepareSendPayload('', ['/tmp/screenshot (1).png'])
      expect(result.displayTxt).toBe('![image](</tmp/screenshot (1).png>)')
    })

    it('mdImageDestToPath is the full inverse of mdImageDest', () => {
      // Already-forward-slashed inputs (slash normalization is one-way).
      for (const p of [
        '/tmp/photo.png',
        '/tmp/screenshot (1).png',
        'C:/Users/John Doe/uploads/shot.png',
        '/tmp/my dir\\.hidden.png',
        '/tmp/a <b>.png',
        '//fileserver/home/me/shot.png',
        '/tmp/photo%20copy.png',
        '/tmp/100%.png',
      ]) {
        expect(mdImageDestToPath(mdImageDest(p))).toBe(p)
      }
    })

    it('mdImageDestToPath preserves an unwrapped legacy destination verbatim', () => {
      // Pre-existing history was written raw: `%20` there is part of the
      // on-disk name, not an encoding.
      expect(mdImageDestToPath('/tmp/photo%20copy.png')).toBe('/tmp/photo%20copy.png')
    })
  })

  it('includes @-referenced files inline and unreferenced as appended tokens', () => {
    const result = prepareSendPayload(
      'see @data.csv for details',
      ['/tmp/data.csv', '/tmp/extra.log'],
    )
    expect(result.filePaths).toContain('/tmp/data.csv')
    expect(result.filePaths).toContain('/tmp/extra.log')
    expect(result.txt).toContain('/tmp/extra.log')
    expect(result.displayTxt).not.toContain('[attached_file')
    expect(result.displayTxt).not.toContain('/tmp/extra.log')
  })

  it('returns empty filePaths when no files pending', () => {
    const result = prepareSendPayload('just text', [])
    expect(result.filePaths).toEqual([])
    expect(result.imgPaths).toEqual([])
  })

  it('replaces @-referenced token inline in txt', () => {
    const result = prepareSendPayload('see @data.csv', ['/tmp/data.csv'])
    expect(result.txt).toContain('[attached_file 1] /tmp/data.csv')
    expect(result.txt).not.toContain('@data.csv')
  })

  it('deduplicates when same file appears twice', () => {
    const result = prepareSendPayload('hello', ['/tmp/a.csv', '/tmp/a.csv'])
    expect(result.filePaths).toEqual(['/tmp/a.csv'])
    expect(result.txt).toContain('[attached_file 1] /tmp/a.csv')
  })

  it('assigns unique token numbers when @-ref is not the first file', () => {
    const result = prepareSendPayload(
      'see @data.csv',
      ['/tmp/extra.log', '/tmp/data.csv'],
    )
    const indices = [...result.txt.matchAll(/\[attached_file (\d+)\]/g)].map(m => m[1])
    expect(indices.length).toBe(2)
    expect(new Set(indices).size).toBe(indices.length)
    expect(result.txt).toContain('[attached_file 1] /tmp/data.csv')
    expect(result.txt).toContain('[attached_file 2] /tmp/extra.log')
    expect(result.filePaths).toEqual(['/tmp/data.csv', '/tmp/extra.log'])
  })
})

describe('restoreQueuedContent (cancel-queued parser fallback)', () => {
  // The primary cancel restore is ChatPage's send-side stash (lossless for
  // every shape). This parser covers reload/other-tab/edited entries, and its
  // contract is strict: claim ONLY provably-lossless shapes, everything else
  // stays verbatim — never worse than the base branch's verbatim restore.

  it('returns plain text unchanged with no files', () => {
    const r = restoreQueuedContent('run the tests')
    expect(r.text).toBe('run the tests')
    expect(r.files).toEqual([])
  })

  it('strips a standalone attachment marker and re-stages its path', () => {
    const { txt } = prepareSendPayload('summarize this report', ['/tmp/q3/report.docx'])
    const r = restoreQueuedContent(txt)
    expect(r.text).toBe('summarize this report')
    expect(r.text).not.toContain('[attached_file')
    expect(r.files).toEqual(['/tmp/q3/report.docx'])
  })

  it('strips several standalone markers and re-stages each path', () => {
    const { txt } = prepareSendPayload('compare all of these', ['/tmp/a.csv', '/tmp/b.log'])
    const r = restoreQueuedContent(txt)
    expect(r.text).toBe('compare all of these')
    expect(new Set(r.files)).toEqual(new Set(['/tmp/a.csv', '/tmp/b.log']))
  })

  it('leaves an embedded (@-mentioned) marker verbatim — its path boundary is not provable', () => {
    // `[attached_file 1] /a/b c` inline in prose: a whitespace-bounded capture
    // truncates a spaced path, staging a nonexistent file and re-sending the
    // wrong one. The stash handles this shape; the parser must not guess.
    const { txt } = prepareSendPayload('see @data.csv for details', ['/tmp/data.csv'])
    const r = restoreQueuedContent(txt)
    expect(r.text).toBe(txt)
    expect(r.files).toEqual([])
  })

  it('restores a producer-form image line as a staged image path', () => {
    const { txt } = prepareSendPayload('what is in this picture', ['/tmp/photo.png'])
    const r = restoreQueuedContent(txt)
    expect(r.text).toBe('what is in this picture')
    expect(r.files).toEqual(['/tmp/photo.png'])
  })

  it('restores an image path containing spaces from its wrapped destination', () => {
    // mdImageDest's <...> wrap gives the destination an exact boundary, so an
    // image is the one spaced-path shape the parser CAN claim losslessly.
    const { txt } = prepareSendPayload('look', ['/tmp/My Shots/pic 1.png'])
    const r = restoreQueuedContent(txt)
    expect(r.text).toBe('look')
    expect(r.files).toEqual(['/tmp/My Shots/pic 1.png'])
  })

  it('leaves an inline image reference the user typed themselves alone', () => {
    const r = restoreQueuedContent('the logo ![image](/tmp/logo.png) sits inline in this sentence')
    expect(r.text).toContain('![image](/tmp/logo.png)')
    expect(r.files).toEqual([])
  })

  it('leaves an own-line relative image from pasted markdown verbatim', () => {
    const pasted = 'review this README excerpt:\n\n## Logo\n\n![image](docs/logo.png)\n\nand the table below'
    const r = restoreQueuedContent(pasted)
    expect(r.text).toBe(pasted)
    expect(r.files).toEqual([])
  })

  it('leaves a relative-path file marker from foreign text verbatim', () => {
    const pasted = 'the transcript said:\n[attached_file 1] docs/spec.md\nwhich was odd'
    const r = restoreQueuedContent(pasted)
    expect(r.text).toBe(pasted)
    expect(r.files).toEqual([])
  })

  it('leaves an own-line marker with a spaced remainder verbatim — the ambiguous shape', () => {
    // Equally a spaced bare-upload path and a line-start mention followed by
    // prose; any claim corrupts one reading, so neither is made.
    const { txt } = prepareSendPayload('summarize this', ['/Users/me/Desktop/My Report.pdf'])
    const r = restoreQueuedContent(txt)
    expect(r.text).toBe(txt)
    expect(r.files).toEqual([])
  })

  it('leaves a line-start mention followed by prose verbatim rather than eating the prose', () => {
    const { txt } = prepareSendPayload('@data.csv is broken', ['/tmp/data.csv'])
    expect(txt).toBe('[attached_file 1] /tmp/data.csv is broken')
    const r = restoreQueuedContent(txt)
    expect(r.text).toBe(txt)
    expect(r.files).toEqual([])
  })

  it('survives malformed marker indices without crashing or consuming them', () => {
    const weird = 'a [attached_file 0] /tmp/x.txt b\n[attached_file 999999999] /tmp/y.txt'
    const r = restoreQueuedContent(weird)
    expect(r.text).toContain('[attached_file 0] /tmp/x.txt')
    expect(r.files).not.toContain('/tmp/x.txt')
    expect(r.files).toEqual(['/tmp/y.txt'])
  })

  it('claims each marker index once — a duplicate N stays verbatim', () => {
    const dup = '[attached_file 1] /tmp/a.txt\n[attached_file 1] /tmp/b.txt'
    const r = restoreQueuedContent(dup)
    expect(r.files).toEqual(['/tmp/a.txt'])
    expect(r.text).toContain('[attached_file 1] /tmp/b.txt')
  })

  it('leaves dir markers verbatim — they sit inline where no boundary is provable', () => {
    const project = '/home/me/proj'
    const { llm } = serializeDirTokens('review @a/src/ carefully', project)
    const r = restoreQueuedContent(llm)
    expect(r.text).toBe(llm)
    expect(r.files).toEqual([])
  })

  it('re-sending the restored state does not double markers', () => {
    const original = prepareSendPayload('check these', ['/tmp/data.csv', '/tmp/extra.log'])
    const r = restoreQueuedContent(original.txt)
    const resent = prepareSendPayload(r.text, r.files)
    expect(resent.txt).toBe(original.txt)
    expect(resent.filePaths).toEqual(original.filePaths)
  })

  it('restores a mixed image + document + text payload completely', () => {
    const { txt } = prepareSendPayload('compare these', ['/tmp/shot.png', '/tmp/data.csv'])
    const r = restoreQueuedContent(txt)
    expect(r.text).toBe('compare these')
    expect(new Set(r.files)).toEqual(new Set(['/tmp/shot.png', '/tmp/data.csv']))
  })

  it('preserves interior whitespace and strips only the blank lines removed markers left', () => {
    const { txt } = prepareSendPayload('line one\n\nline  two', ['/tmp/photo.png'])
    const r = restoreQueuedContent(txt)
    expect(r.text).toBe('line one\n\nline  two')
  })
})
