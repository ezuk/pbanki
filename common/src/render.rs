//! Lays out card HTML as pages of monospace text lines.
//!
//! Supports the formatting that survives on e-ink: bold, italic, line breaks,
//! paragraphs, lists, headings, horizontal rules and cloze highlights.

use unicode_width::UnicodeWidthChar;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Style {
    pub bold: bool,
    pub italic: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Run {
    pub text: String,
    pub style: Style,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Line {
    pub runs: Vec<Run>,
    pub rule: bool,
    pub centered: bool,
}

impl Line {
    pub fn text(&self) -> String {
        self.runs.iter().map(|r| r.text.as_str()).collect()
    }
}

pub type Page = Vec<Line>;

#[derive(Debug, Clone, PartialEq, Eq)]
enum Block {
    Paragraph(Vec<Run>),
    Rule,
    Blank,
}

/// Number of monospace columns a character occupies.
fn char_columns(c: char) -> usize {
    c.width().unwrap_or(0)
}

fn text_columns(text: &str) -> usize {
    text.chars().map(char_columns).sum()
}

/// Lays out `html` into pages of at most `rows` lines of `cols` columns.
pub fn layout(html: &str, cols: usize, rows: usize) -> Vec<Page> {
    let cols = cols.max(1);
    let rows = rows.max(1);

    let mut lines = Vec::new();
    for block in parse_blocks(html) {
        match block {
            Block::Rule => lines.push(Line {
                rule: true,
                ..Default::default()
            }),
            Block::Blank => lines.push(Line::default()),
            Block::Paragraph(runs) => lines.extend(wrap(&runs, cols)),
        }
    }

    let mut pages: Vec<Page> = Vec::new();
    let mut page = Page::new();
    for line in lines {
        let blank = line.runs.is_empty() && !line.rule;
        if page.len() == rows {
            pages.push(std::mem::take(&mut page));
        }
        if blank && page.is_empty() {
            continue;
        }
        page.push(line);
    }
    while page.last().is_some_and(|l| l.runs.is_empty() && !l.rule) {
        page.pop();
    }
    if !page.is_empty() || pages.is_empty() {
        pages.push(page);
    }
    pages
}

/// Index of the first page containing a horizontal rule.
pub fn first_rule_page(pages: &[Page]) -> Option<usize> {
    pages.iter().position(|p| p.iter().any(|l| l.rule))
}

/// Greedy word wrap. Paragraphs that fit on one line are centered.
fn wrap(runs: &[Run], cols: usize) -> Vec<Line> {
    // Split into words, each a list of styled pieces
    let mut words: Vec<Vec<Run>> = Vec::new();
    let mut current: Vec<Run> = Vec::new();
    for run in runs {
        for (i, part) in run.text.split(' ').enumerate() {
            if i > 0 && !current.is_empty() {
                words.push(std::mem::take(&mut current));
            }
            if !part.is_empty() {
                current.push(Run {
                    text: part.to_owned(),
                    style: run.style,
                });
            }
        }
    }
    if !current.is_empty() {
        words.push(current);
    }

    let mut lines: Vec<Line> = Vec::new();
    let mut line = Line::default();
    let mut width = 0;

    for word in words {
        let word_width: usize = word.iter().map(|r| text_columns(&r.text)).sum();
        let space = usize::from(width > 0);

        if width > 0 && width + space + word_width > cols {
            lines.push(std::mem::take(&mut line));
            width = 0;
        }

        if word_width > cols {
            // Break overlong words across lines
            for run in word {
                for c in run.text.chars() {
                    let c_width = char_columns(c);
                    if width + c_width > cols && width > 0 {
                        lines.push(std::mem::take(&mut line));
                        width = 0;
                    }
                    push_text(&mut line, &c.to_string(), run.style);
                    width += c_width;
                }
            }
            continue;
        }

        if width > 0 {
            let prev = line.runs.last().map(|r| r.style).unwrap_or_default();
            let next = word[0].style;
            let style = Style {
                bold: prev.bold && next.bold,
                italic: prev.italic && next.italic,
            };
            push_text(&mut line, " ", style);
            width += 1;
        }
        for run in word {
            push_text(&mut line, &run.text, run.style);
        }
        width += word_width;
    }
    if !line.runs.is_empty() {
        lines.push(line);
    }

    if lines.len() == 1 {
        lines[0].centered = true;
    }
    lines
}

fn push_text(line: &mut Line, text: &str, style: Style) {
    match line.runs.last_mut() {
        Some(last) if last.style == style => last.text.push_str(text),
        _ => line.runs.push(Run {
            text: text.to_owned(),
            style,
        }),
    }
}

#[derive(Default)]
struct BlockBuilder {
    blocks: Vec<Block>,
    runs: Vec<Run>,
}

impl BlockBuilder {
    fn text(&mut self, text: &str, style: Style) {
        let mut normalized = String::with_capacity(text.len());
        for c in text.chars() {
            if c.is_whitespace() && c != '\u{a0}' {
                if !normalized.ends_with(' ') {
                    normalized.push(' ');
                }
            } else {
                normalized.push(if c == '\u{a0}' { ' ' } else { c });
            }
        }
        // Collapse spaces across run boundaries
        let at_line_start = self.runs.iter().all(|r| r.text.trim().is_empty());
        let prev_space = self.runs.last().is_none_or(|r| r.text.ends_with(' '));
        let normalized = if prev_space || at_line_start {
            normalized.trim_start_matches(' ').to_owned()
        } else {
            normalized
        };
        if normalized.is_empty() {
            return;
        }
        match self.runs.last_mut() {
            Some(last) if last.style == style => last.text.push_str(&normalized),
            _ => self.runs.push(Run {
                text: normalized,
                style,
            }),
        }
    }

    /// Ends the current paragraph, if any.
    fn end_paragraph(&mut self) {
        if let Some(last) = self.runs.last_mut() {
            let trimmed = last.text.trim_end().len();
            last.text.truncate(trimmed);
        }
        self.runs.retain(|r| !r.text.is_empty());
        if !self.runs.is_empty() {
            self.blocks
                .push(Block::Paragraph(std::mem::take(&mut self.runs)));
        }
    }

    /// Ends the current line; an empty line becomes a blank line.
    fn line_break(&mut self) {
        if self.runs.is_empty() {
            self.blank();
        } else {
            self.end_paragraph();
        }
    }

    fn blank(&mut self) {
        self.end_paragraph();
        if !matches!(self.blocks.last(), None | Some(Block::Blank)) {
            self.blocks.push(Block::Blank);
        }
    }

    fn rule(&mut self) {
        self.end_paragraph();
        if self.blocks.last() == Some(&Block::Blank) {
            self.blocks.pop();
        }
        self.blocks.push(Block::Rule);
    }

    fn finish(mut self) -> Vec<Block> {
        self.end_paragraph();
        while self.blocks.last() == Some(&Block::Blank) {
            self.blocks.pop();
        }
        self.blocks
    }
}

struct Tag<'a> {
    name: String,
    closing: bool,
    attrs: &'a str,
}

fn parse_tag(inner: &str) -> Tag<'_> {
    let closing = inner.starts_with('/');
    let inner = inner.trim_start_matches('/').trim_end_matches('/');
    let name_end = inner
        .find(|c: char| c.is_whitespace())
        .unwrap_or(inner.len());
    Tag {
        name: inner[..name_end].to_ascii_lowercase(),
        closing,
        attrs: &inner[name_end..],
    }
}

/// Style a tag applies to its contents, if it is a styling tag.
fn tag_style(tag: &Tag, parent: Style) -> Option<Style> {
    let attrs = tag.attrs.to_ascii_lowercase();
    let mut style = parent;
    match tag.name.as_str() {
        "b" | "strong" | "h1" | "h2" | "h3" | "h4" | "h5" | "h6" => style.bold = true,
        "i" | "em" => style.italic = true,
        "span" | "font" | "div" | "p" => {
            if class_list(&attrs).any(|class| class == "cloze") {
                style.bold = true;
            }
            if attrs.contains("font-weight: bold")
                || attrs.contains("font-weight:bold")
                || attrs.contains("font-weight: 700")
                || attrs.contains("font-weight:700")
            {
                style.bold = true;
            }
            if attrs.contains("font-style: italic") || attrs.contains("font-style:italic") {
                style.italic = true;
            }
        }
        _ => return None,
    }
    Some(style)
}

/// Classes in a tag's `class="..."` attribute.
fn class_list(attrs: &str) -> impl Iterator<Item = &str> {
    let value = attrs
        .find("class=")
        .map(|i| {
            let rest = &attrs[i + "class=".len()..];
            let quote = rest.chars().next().filter(|c| *c == '"' || *c == '\'');
            match quote {
                Some(q) => rest[1..].split(q).next().unwrap_or(""),
                None => rest.split_whitespace().next().unwrap_or(""),
            }
        })
        .unwrap_or("");
    value.split_whitespace()
}

fn is_void(name: &str) -> bool {
    matches!(
        name,
        "br" | "hr" | "img" | "input" | "meta" | "link" | "wbr"
    )
}

fn parse_blocks(html: &str) -> Vec<Block> {
    let html = anki::card_rendering::strip_av_tags(html);
    let mut builder = BlockBuilder::default();
    let mut styles: Vec<(String, Style)> = Vec::new();
    let mut list_depth: Vec<Option<usize>> = Vec::new();
    let mut rest = html.as_str();

    let current = |styles: &[(String, Style)]| styles.last().map(|s| s.1).unwrap_or_default();

    while !rest.is_empty() {
        let Some(lt) = rest.find('<') else {
            builder.text(&anki::text::decode_entities(rest), current(&styles));
            break;
        };
        if lt > 0 {
            builder.text(&anki::text::decode_entities(&rest[..lt]), current(&styles));
        }
        rest = &rest[lt..];

        if rest.starts_with("<!--") {
            rest = rest.find("-->").map_or("", |end| &rest[end + 3..]);
            continue;
        }
        let Some(gt) = rest.find('>') else {
            builder.text(&anki::text::decode_entities(rest), current(&styles));
            break;
        };
        let tag = parse_tag(&rest[1..gt]);
        rest = &rest[gt + 1..];

        if !tag.closing && matches!(tag.name.as_str(), "style" | "script" | "head" | "title") {
            let end = format!("</{}", tag.name);
            let lower = rest.to_ascii_lowercase();
            rest = lower
                .find(&end)
                .and_then(|i| rest[i..].find('>').map(|j| &rest[i + j + 1..]))
                .unwrap_or("");
            continue;
        }

        match (tag.name.as_str(), tag.closing) {
            ("br", _) => builder.line_break(),
            ("hr", _) => builder.rule(),
            ("img", _) => builder.text(
                " [image] ",
                Style {
                    italic: true,
                    ..current(&styles)
                },
            ),
            ("p" | "h1" | "h2" | "h3" | "h4" | "h5" | "h6" | "blockquote" | "pre", _) => {
                builder.blank()
            }
            ("div" | "tr" | "table" | "dl" | "dt" | "dd", _) => builder.end_paragraph(),
            ("td" | "th", true) => builder.text(" ", current(&styles)),
            ("ul", false) => {
                builder.end_paragraph();
                list_depth.push(None);
            }
            ("ol", false) => {
                builder.end_paragraph();
                list_depth.push(Some(0));
            }
            ("ul" | "ol", true) => {
                builder.end_paragraph();
                list_depth.pop();
            }
            ("li", false) => {
                builder.end_paragraph();
                let indent = "  ".repeat(list_depth.len().saturating_sub(1));
                let marker = match list_depth.last_mut() {
                    Some(Some(n)) => {
                        *n += 1;
                        format!("{indent}{n}. ")
                    }
                    _ => format!("{indent}• "),
                };
                builder.runs.push(Run {
                    text: marker,
                    style: current(&styles),
                });
            }
            ("li", true) => builder.end_paragraph(),
            _ => {}
        }

        if is_void(&tag.name) {
            continue;
        }
        if tag.closing {
            if let Some(pos) = styles.iter().rposition(|(name, _)| *name == tag.name) {
                styles.truncate(pos);
            }
        } else if let Some(style) = tag_style(&tag, current(&styles)) {
            styles.push((tag.name.clone(), style));
        }
    }

    builder.finish()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn texts(pages: &[Page]) -> Vec<Vec<String>> {
        pages
            .iter()
            .map(|p| {
                p.iter()
                    .map(|l| if l.rule { "---".into() } else { l.text() })
                    .collect()
            })
            .collect()
    }

    fn one_page(html: &str, cols: usize) -> Vec<String> {
        let pages = layout(html, cols, 100);
        assert_eq!(pages.len(), 1, "{pages:?}");
        texts(&pages).remove(0)
    }

    #[test]
    fn plain_text_wraps_at_columns() {
        assert_eq!(
            one_page("the quick brown fox jumps", 10),
            vec!["the quick", "brown fox", "jumps"]
        );
    }

    #[test]
    fn short_paragraph_is_centered_long_is_not() {
        let pages = layout("short<br>a much longer paragraph", 10, 10);
        assert!(pages[0][0].centered);
        assert!(!pages[0][1].centered);
    }

    #[test]
    fn line_breaks_and_divs_start_new_lines() {
        assert_eq!(
            one_page("one<br>two<div>three</div>four", 40),
            vec!["one", "two", "three", "four"]
        );
    }

    #[test]
    fn double_break_gives_blank_line() {
        assert_eq!(one_page("a<br><br>b", 40), vec!["a", "", "b"]);
    }

    #[test]
    fn hr_becomes_rule_between_front_and_back() {
        let html = "<style>.card{}</style>Front<hr id=answer>Back";
        assert_eq!(one_page(html, 40), vec!["Front", "---", "Back"]);
    }

    #[test]
    fn styles_are_kept() {
        let pages = layout("a <b>bold</b> and <i>italic <b>both</b></i>", 80, 5);
        let runs = &pages[0][0].runs;
        let styled: Vec<_> = runs
            .iter()
            .map(|r| (r.text.as_str(), r.style.bold, r.style.italic))
            .collect();
        assert_eq!(
            styled,
            vec![
                ("a ", false, false),
                ("bold", true, false),
                (" and ", false, false),
                ("italic ", false, true),
                ("both", true, true),
            ]
        );
    }

    #[test]
    fn cloze_span_is_bold() {
        let pages = layout(r#"The <span class="cloze">[...]</span> river"#, 80, 5);
        assert!(
            pages[0][0]
                .runs
                .iter()
                .any(|r| r.text == "[...]" && r.style.bold)
        );
    }

    #[test]
    fn inactive_cloze_is_plain() {
        let pages = layout(
            r#"<span class="cloze-inactive" data-ordinal="2">Paris</span>"#,
            80,
            5,
        );
        assert!(pages[0][0].runs.iter().all(|r| !r.style.bold));
    }

    #[test]
    fn decodes_entities_and_collapses_whitespace() {
        assert_eq!(
            one_page("a &amp;\n\n  b&nbsp;&lt;c&gt;", 40),
            vec!["a & b <c>"]
        );
    }

    #[test]
    fn lists_get_markers() {
        assert_eq!(
            one_page(
                "<ul><li>x</li><li>y</li></ul><ol><li>first</li><li>second</li></ol>",
                40
            ),
            vec!["• x", "• y", "1. first", "2. second"]
        );
    }

    #[test]
    fn strips_sound_tags_and_scripts() {
        assert_eq!(
            one_page("Hi [sound:a.mp3]<script>alert(1)</script><!-- c -->", 40),
            vec!["Hi"]
        );
    }

    #[test]
    fn images_get_placeholder() {
        assert_eq!(
            one_page(r#"See <img src="x.png">"#, 40),
            vec!["See [image]"]
        );
    }

    #[test]
    fn wide_characters_count_double() {
        assert_eq!(one_page("日本語日本語", 6), vec!["日本語", "日本語"]);
        assert_eq!(one_page("Здравствуйте", 12), vec!["Здравствуйте"]);
    }

    #[test]
    fn long_words_are_broken() {
        assert_eq!(one_page("abcdefghij", 4), vec!["abcd", "efgh", "ij"]);
    }

    #[test]
    fn paginates_by_rows_without_blank_page_starts() {
        let pages = layout("a<br>b<br><br>c<br>d", 40, 2);
        assert_eq!(texts(&pages), vec![vec!["a", "b"], vec!["c", "d"]]);
    }

    #[test]
    fn finds_answer_page() {
        let pages = layout("q1<br>q2<br>q3<hr>a", 40, 2);
        assert_eq!(first_rule_page(&pages), Some(1));
    }

    #[test]
    fn empty_html_gives_one_empty_page() {
        assert_eq!(layout("", 10, 10), vec![Page::new()]);
        assert_eq!(layout("<div></div>", 10, 10), vec![Page::new()]);
    }
}
