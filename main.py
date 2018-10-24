import os
import shutil
import argparse
import re
from PIL import Image


# TODO: strip inline comments
# TODO: traverse \usepackage to check local packages
# TODO: also strip comments from included files


_RE_INCLUDE_GRAPHICS = re.compile(r'\\includegraphics(\[.*\])?{(.*?)}')
_RE_OVERPIC = re.compile(r'\\begin{overpic}(\[.*\])?{(.*?)}')
_RE_INPUT = re.compile(r'\\input{(.*?)}')

_EXTS = ('.jpg', '.pdf', '.tex', '.png')


def copy_latex(flags):
    if os.path.isdir(flags.out_dir):
        assert_exc(flags.force, '{} exists, use --force'.format(flags.out_dir))
        _rmtree_semi_safe(flags.out_dir)
    os.makedirs(flags.out_dir, exist_ok=True)
    c = Copier(flags.out_dir, flags.encodings, convert_to_jpg=flags.convert_to_jpg)
    c.copy_all(flags.main_file, current_dir=os.path.dirname(flags.main_file))
    for p in flags.other_files:
        c.copy(os.path.dirname(flags.main_file), p)

    sizes = [(os.path.getsize(p) // 1028, p) for p in c.copied_files]
    print('Biggest files:')
    print('\n'.join('{}kB: {}'.format(s, p) for s, p in sorted(sizes, reverse=True)[:10]))
    print('Total: {}kB'.format(sum(s for s, _ in sizes)))

    for p in c.copied_files:
        if p.endswith('.tex'):
            strip_comments(p)


class Copier(object):
    def __init__(self, out_dir, encodings, convert_to_jpg=True):
        self.out_dir = out_dir
        self.copied_files = []
        self.encodings = encodings
        self.convert_to_jpg = convert_to_jpg

    def copy_all(self, latex_file_p, current_dir):
        self.copy(current_dir, latex_file_p)
        for encoding in self.encodings:
            try:
                self._read_and_copy(current_dir, latex_file_p, encoding)
                break  # successfully read
            except UnicodeDecodeError as e:
                print('Error while reading {} with {}: {}'.format(latex_file_p, encoding, e))
        else:  # no-break, i.e., never sucessfully read
            print('ERR: Unable to read {} with encodings {}. Pass --encodings'.format(latex_file_p, self.encodings))

    def _read_and_copy(self, current_dir, latex_file_p, encoding):
        """
        :param current_dir:
        :param latex_file_p:
        :raise UnicodeDecodeError if file is in wrong encoding
        :return:
        """
        with open(latex_file_p, 'r', encoding=encoding) as f:
            for l in f:
                if l.strip().startswith('%'):  # comment
                    continue
                m = _RE_INCLUDE_GRAPHICS.search(l) or _RE_OVERPIC.search(l)
                if m:
                    self.copy(current_dir, m.group(2))
                    continue
                m = _RE_INPUT.search(l)
                if m:
                    input_latex_file_name = m.group(1)
                    # TODO: currently not supported
                    assert '/' not in input_latex_file_name, input_latex_file_name
                    input_latex_file_p = os.path.join(current_dir, input_latex_file_name)
                    self.copy_all(input_latex_file_p, current_dir)  # recurse
                    continue

    def copy(self, current_dir, p):
        p, in_latex = self.get_actual_p(os.path.join(current_dir, p))
        out_p = p.replace(current_dir, self.out_dir)
        assert out_p.startswith(self.out_dir)

        if p.endswith('.png') and self.convert_to_jpg:
            p_name = os.path.basename(p)
            assert_exc(not in_latex,
                       '--convert_to_jpg specified but {} is with extension is LaTeX, '
                       'you should remove extension in LaTeX source! I.e. s/{}/{}'.format(
                               p_name, p_name, os.path.splitext(p_name)[0]))
            out_p = out_p.replace('.png', '.jpg')
            print('Converted!', out_p)
            os.makedirs(os.path.dirname(out_p), exist_ok=True)
            Image.open(p).save(out_p, quality=95)
        else:
            try:
                shutil.copy(p, out_p)
            except FileNotFoundError as e:
                os.makedirs(os.path.dirname(out_p), exist_ok=True)
                shutil.copy(p, out_p)
        self.copied_files.append(out_p)

    @staticmethod
    def get_actual_p(p):
        """ :returns tuple (p', in_latex), where in_latex is a bool indicating whether the extension is specified in
        the latex."""
        if os.path.isfile(p):
            return p, True
        for ext in _EXTS:
            if os.path.isfile(p + ext):
                return p + ext, False
        raise ValueError('Did not find file starting with {} and ext in {}'.format(p, _EXTS))


def strip_comments(p):
    """ Remove unneeded comments from LaTeX file `p`. """
    with open(p, 'r', encoding='utf-8') as fin:
        lines = fin.readlines()

    p_stripped = p + 'stripped'
    with open(p_stripped, 'w') as fout:
        for i, l in enumerate(lines):
            m = re.search(r'((...)?%(.*$))', l)
            if l.startswith('%'):  # comment line, remove
                continue
            # if a line ends with % only, the % is used for layout -> keep
            # \% is percentage -> keep
            if (l.rstrip().endswith('%')) or (r'\%' in l):
                fout.write(l)
                continue
            # lines containing % in the middle are ignored for now
            # TODO
            if '%' in l:
                print('Remaining Comment on L{}: {}'.format(i, m.group(1)))
            fout.write(l)

    os.rename(p_stripped, p)


# Helpers ----------------------------------------------------------------------


def assert_exc(cond, msg=None, exc=ValueError):
    if not cond:
        raise exc(msg)


def _rmtree_semi_safe(out_dir, max_size_mb=20):
    assert_exc(_get_size(out_dir, max_size_mb * 1024 * 1024) is not None,
               'Will not rm -rf {}, too big. Please delete manually.'.format(out_dir))
    shutil.rmtree(out_dir)


def _get_size(p, max_size):
    """ Get size of directory `p`, stop if `max_size` is reached. """
    total_size = 0
    for dirpath, dirnames, filenames in os.walk(p):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            total_size += os.path.getsize(fp)
            if total_size > max_size:
                return None
    return total_size


# Main -------------------------------------------------------------------------


def main():
    p = argparse.ArgumentParser()
    p.add_argument('main_file', help=r'Main Latex file, will copy and also all files included with \input, '
                                     r'\includegraphics, and \overpic. All files in \input are recursively parsed and '
                                     r'treated like the main file.')
    p.add_argument('other_files', nargs='*', help='Other files not included in the main_file, will copy '
                                                  '(no recursion).')
    p.add_argument('--out_dir', '-o', help='Where to store files.')
    p.add_argument('--encodings', default=['utf-8'], nargs='+', help='Encodings to try when opening .tex files')
    p.add_argument('--force', '-f', action='store_true', help='If given, delete and re-create OUT_DIR. '
                                                              'WARNING: Calls rm -rf OUT_DIR.')
    p.add_argument('--convert_to_jpg', '-jpg', action='store_true', help='If given, convert .pngs to .jpg')
    flags = p.parse_args()

    if flags.out_dir is None:
        flags.out_dir = os.path.dirname(flags.main_file) + '_arXiv'

    copy_latex(flags)


if __name__ == '__main__':
    main()
