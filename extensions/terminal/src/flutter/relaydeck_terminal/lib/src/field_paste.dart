// Pasting text into the focused Flutter text field from a browser paste event,
// without the async clipboard API: the same insertion `EditableTextState.pasteText`
// performs once it has the text (editable_text.dart `_pasteText`).
import 'dart:math' as math;

import 'package:flutter/widgets.dart';

/// The Flutter text field holding the primary focus, if any.
EditableTextState? focusedEditable() {
  final context = FocusManager.instance.primaryFocus?.context;
  return context?.findAncestorStateOfType<EditableTextState>();
}

/// Replaces the field's selection with [text] and leaves a collapsed caret after
/// it; the field's own formatters and change listeners run as for a keyboard
/// paste. False when the field cannot take a paste.
bool pasteIntoField(EditableTextState field, String text) {
  if (field.widget.readOnly) return false;
  final value = field.textEditingValue;
  final selection = value.selection;
  if (!selection.isValid) return false;
  final end = math.max(selection.baseOffset, selection.extentOffset);
  field.userUpdateTextEditingValue(
    value
        .copyWith(selection: TextSelection.collapsed(offset: end))
        .replaced(selection, text),
    SelectionChangedCause.keyboard,
  );
  return true;
}
