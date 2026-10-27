import sys


class _OptionalImageUtilsBlocker:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'qfluentwidgets.common.image_utils':
            raise ModuleNotFoundError('optional acrylic image utilities are disabled')
        return None


_BLOCKER = _OptionalImageUtilsBlocker()


def block_optional_imports() -> None:
    if _BLOCKER not in sys.meta_path:
        sys.meta_path.insert(0, _BLOCKER)
