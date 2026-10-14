use std::rc::Rc;

use anki_proto::decks::DeckTreeNode;
use slint::{ModelRc, SharedString, VecModel};

use crate::render::Page;
use crate::{DeckNode, Line, Span};

/// Flattens the deck tree into list rows, leaving out children of collapsed decks.
pub fn flatten_tree(node: &DeckTreeNode) -> Rc<VecModel<DeckNode>> {
    let result = Rc::new(VecModel::<DeckNode>::default());
    flatten_tree_recursive(node, &result);
    result
}

fn flatten_tree_recursive(node: &DeckTreeNode, result: &Rc<VecModel<DeckNode>>) {
    for child in &node.children {
        result.push(DeckNode {
            id: child.deck_id,
            name: child.name.clone().into(),
            level: child.level as i32,
            collapsed: child.collapsed,
            new: child.new_count as i32,
            learn: child.learn_count as i32,
            due: child.review_count as i32,
            has_children: !child.children.is_empty(),
        });
        if !child.collapsed {
            flatten_tree_recursive(child, result);
        }
    }
}

/// Converts laid-out pages into the UI's page model.
pub fn pages_model(pages: Vec<Page>) -> ModelRc<crate::Page> {
    let pages: Vec<crate::Page> = pages
        .into_iter()
        .map(|lines| crate::Page {
            lines: ModelRc::new(VecModel::from(
                lines
                    .into_iter()
                    .map(|line| Line {
                        spans: ModelRc::new(VecModel::from(
                            line.runs
                                .into_iter()
                                .map(|run| Span {
                                    text: SharedString::from(run.text),
                                    bold: run.style.bold,
                                    italic: run.style.italic,
                                })
                                .collect::<Vec<_>>(),
                        )),
                        rule: line.rule,
                        centered: line.centered,
                    })
                    .collect::<Vec<_>>(),
            )),
        })
        .collect();
    ModelRc::new(VecModel::from(pages))
}

#[cfg(test)]
mod tests {
    use super::*;
    use slint::Model;

    fn leaf(id: i64, name: &str, level: u32) -> DeckTreeNode {
        DeckTreeNode {
            deck_id: id,
            name: name.into(),
            level,
            ..Default::default()
        }
    }

    #[test]
    fn flattens_deck_tree_in_order() {
        let root = DeckTreeNode {
            children: vec![
                DeckTreeNode {
                    children: vec![leaf(2, "Child", 2)],
                    review_count: 5,
                    ..leaf(1, "Parent", 1)
                },
                leaf(3, "Other", 1),
            ],
            ..Default::default()
        };

        let model = flatten_tree(&root);
        let ids: Vec<_> = model.iter().map(|n| n.id).collect();
        assert_eq!(ids, vec![1, 2, 3]);

        let parent = model.row_data(0).unwrap();
        assert!(parent.has_children);
        assert_eq!(parent.due, 5);
        assert_eq!(parent.name, "Parent");
    }

    #[test]
    fn hides_all_descendants_of_collapsed_decks() {
        let root = DeckTreeNode {
            children: vec![DeckTreeNode {
                collapsed: true,
                children: vec![DeckTreeNode {
                    children: vec![leaf(3, "Grandchild", 3)],
                    ..leaf(2, "Child", 2)
                }],
                ..leaf(1, "Parent", 1)
            }],
            ..Default::default()
        };

        let ids: Vec<_> = flatten_tree(&root).iter().map(|n| n.id).collect();
        assert_eq!(ids, vec![1]);
    }

    #[test]
    fn converts_pages_to_ui_model() {
        let pages = crate::render::layout("<b>Hi</b> there<hr>x", 40, 10);
        let model = pages_model(pages);
        let page = model.row_data(0).unwrap();
        let first = page.lines.row_data(0).unwrap();
        let span = first.spans.row_data(0).unwrap();
        assert_eq!(span.text, "Hi");
        assert!(span.bold);
        assert!(page.lines.row_data(1).unwrap().rule);
    }
}
