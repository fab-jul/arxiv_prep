import glob
import os
import subprocess
import sys
import shutil
import argparse
import re
from collections import namedtuple
from contextlib import contextmanager

from PIL import Image


# TODO: traverse \usepackage to check local packages
# TODO: don't continue recursion after \end{document}
# TODO: automatically tar gz

# TODO: strip before parse


_RE_INCLUDE_GRAPHICS = re.compile(r'\\includegraphics(\[.*\])?{(.*?)}')
_RE_OVERPIC = re.compile(r'\\begin{overpic}(\[.*\])?{(.*?)}')
# both regexes have group 2: path of image to be included
_IMAGE_INCLUDE_RE = [_RE_INCLUDE_GRAPHICS, _RE_OVERPIC]
# Groups: 2: command name, 4: number of args
_RE_NEWCOMMAND = re.compile(r'\\(re)?newcommand{?(.*?)}?(\[(\d+)\])?{')

# filename_group gives which regex group to check
# must_exist: if True, an exception is thrown if the file does not exist.
FileSpec = namedtuple('FileSpec', ['regex', 'filename_group', 'ext', 'must_exist'])

_RECURSIVE_INCLUDE = [
    FileSpec(re.compile(r'\\input{(.*?)}'), 1, '.tex', True),
    FileSpec(re.compile(r'\\subfile{(.*?)}'), 1, '.tex', True),
    FileSpec(re.compile(r'\\bibliographystyle{(.*?)}'), 1, '.bst', True),
    FileSpec(re.compile(r'\\bibliography{(.*?)}'), 1, '.bib', True),
    # here, must_exist=False, since \usepackage{X} might include a system package.
    FileSpec(re.compile(r'\\usepackage(\[.*\])?{(.*?)}'), 2, '.sty', False),
]

_EXTS = ('.jpg', '.pdf', '.tex', '.png')


NewCommand = namedtuple('NewCommand', ['command_name', 'command'])


def _replace_all(s, rep):
    assert all(isinstance(v, str) for v in rep.values())
    rep = {re.escape(k): v for k, v in rep.items()}
    pattern = re.compile('|'.join(rep.keys()))
    return pattern.sub(lambda m: rep[re.escape(m.group(0))], s)


def _consume_until_closing_bracket(l, f_iter, max_lookahead=1000):
    """
    Given a line `l` and a file iterator `f_iter`, consume lines from `f_iter` until all brackets in `l` are
    closed.
    :return: the complete command started in line l
    """
    l = strip_comments_from_line(l)
    num_open, num_close = l.count('{'), l.count('}')
    if num_open == num_close:  # assuming that we do not have open brackets from previous lines...
        return l
    if num_close > num_open:
        raise ValueError('Did not expect line to have more closing brackets than opening ({})'.format(l))
    consummed_lines = 0
    while num_close != num_open:
        consummed_lines += 1
        if consummed_lines > max_lookahead:
            raise ValueError('Could not complete command starting with {}'.format(l[:100]))
        _, next_l = next(f_iter)
        next_l = strip_comments_from_line(next_l)
        if not next_l:
            continue
        l += next_l
        num_open += next_l.count('{')
        num_close += next_l.count('}')
    return l


class ParseLineException(Exception):
    pass

class FileSearchException(Exception):
    pass

class InvalidIncludeException(Exception):
    pass


def copy_latex(flags):
    # TODO: working dir
    # for p in flags.other_files:
    #     if not os.path.isfile(p):
    #         print('Extra file {} does not exist'.format(p))
    #         return 1
    if os.path.isdir(flags.out_dir):
        assert_exc(flags.force, '{} exists, use --force'.format(flags.out_dir))
        _rmtree_semi_safe(flags.out_dir)
    os.makedirs(flags.out_dir, exist_ok=True)
    c = Copier(flags.out_dir, flags.encodings, convert_to_jpg=flags.convert_to_jpg)
    try:
        c.copy_all(flags.main_file, current_dir=os.path.dirname(flags.main_file))
    except ParseLineException as e:
        print('Error: {}'.format(e))
        return 1

    # for p in flags.other_files:
    #     c.copy(os.path.dirname(flags.main_file), p)
    #
    for p in c.copied_files:
        if p.endswith('.tex'):
            strip_comments(p)

    main_file_out = os.path.join(flags.out_dir, os.path.basename(flags.main_file))

    if flags.store_git_hash:
        git_hash = _get_git_hash(os.path.dirname(flags.main_file))
        if git_hash:
            print('Writing git hash {}...'.format(git_hash))
            _insert_in_file(main_file_out, text='% ' + git_hash)

    sizes = [(os.path.getsize(p) // 1028, p) for p in c.copied_files]
    print('Biggest files:')
    print('\n'.join('{}kB: {}'.format(s, p) for s, p in sorted(sizes, reverse=True)[:10]))
    print('Total: {}kB'.format(sum(s for s, _ in sizes)))

    if flags.rename:
        _, ext = os.path.splitext(flags.rename)
        if not ext:
            flags.rename += '.tex'
        os.rename(main_file_out,
                  os.path.join(flags.out_dir, flags.rename))
    return 0


class Copier(object):
    def __init__(self, out_dir, encodings, convert_to_jpg=True):
        self.out_dir = out_dir
        self.copied_files = []
        self.encodings = encodings
        self.convert_to_jpg = convert_to_jpg
        self.commands = {}  # command_name -> command
        self.command_regexes = []

    def copy_all(self, latex_file_p, current_dir):
        """
        Recursively parse file at `latex_file_p`.
        """
        self.copy(current_dir, latex_file_p)
        for encoding in self.encodings:
            try:
                self._read_and_copy(current_dir, latex_file_p, encoding)
                break  # successfully read
            except UnicodeDecodeError as e:   # TODO: always unicode error?
                print('Error while reading {} with {}: {}'.format(latex_file_p, encoding, e))
        else:  # no-break, i.e., never sucessfully read
            print('ERR: Unable to read {} with encodings {}. Pass --encodings'.format(latex_file_p, self.encodings))

    def _consume_and_parse_newcommand(self, current_line, file_iter):
        m = _RE_NEWCOMMAND.search(current_line)
        assert m is not None
        command_name, num_args = m.group(2), m.group(4)
        command = _consume_until_closing_bracket(current_line, file_iter)
        # if the command does not include images, we do not care!
        if not Copier._included_images(command) and not Copier._contains_include_statement(command):
            return
        print('Caching command {}, as it references images...'.format(command_name))
        self.commands[command_name] = command
        self.command_regexes.append(re.compile('(\\'+command_name + ')' + r'{(.*?)}' * int(num_args or 0)))

    # TODO: hard to find which commands are used. because \cvpr and \cvpr{1}{2} and \cvpr{1%\n}{2} are all possible
    def _files_included_using_commands(self, current_line):
        imgs, ps = [], []
        for r in self.command_regexes:
            m = r.search(current_line)
            if m:  # this line calls a command that includes an image
                # split groups, first group is command name, remaining are the arguments
                # TODO: test with command that has 0 args
                command_name, args = m.groups()[0], m.groups()[1:]
                # get command, substitue arguments to find actual paths
                command = self.commands[command_name]
                subsituted_command = _replace_all(command, {'#' + str(i+1): arg for i, arg in enumerate(args)})
                # find actual paths in command
                included_images = Copier._included_images(subsituted_command)
                if included_images:
                    print('Found {} images included via {}:\n\t{}'.format(
                            len(included_images), command_name, '\n\t'.join(included_images)))
                    imgs += included_images
                # find paths
                # TODO: this is where it breaks for now! i.e. should return multiple
                # and then join with whatever is read
                included_paths = Copier._included_source_file()
        return res

    def _read_and_copy(self, current_dir, latex_file_p, encoding):
        """
        Copy file `latex_file_p` and refered images, recursively copy all files included via \input
        :param current_dir: current directory, where images are searched
        :param latex_file_p: file to be parsed, full file path!
        :raise UnicodeDecodeError if file is in wrong encoding
        """
        print(latex_file_p)
        with open(latex_file_p, 'r', encoding=encoding) as f:
            f_iter = enumerate(f)
            for line_number, l in f_iter:
                if l.strip().startswith('%'):  # comment
                    continue
                m = _RE_NEWCOMMAND.search(l)
                if m:  # parse newcommand
                    self._consume_and_parse_newcommand(l, f_iter)
                    continue
                # get images included with commands
                img_ps = self._images_included_using_commands(l)
                # get images included directly
                img_ps += Copier._included_images(l)
                # at this point, img_ps might be [] or a list of paths that must be copied
                for img_p in img_ps:
                    try:
                        self.copy(current_dir, img_p)
                        continue
                    except FileSearchException as e:
                        Copier._raise_with_info(line_number, latex_file_p, l, e)
                try:
                    input_latex_file_name = self._included_source_file(current_dir, l)  # can raise InvalidIncludeException
                    if input_latex_file_name:  # recursion
                        print('Recursing into', input_latex_file_name)
                        # TODO: currently not supported
                        # assert '/' not in input_latex_file_name, input_latex_file_name
                        input_latex_file_p = os.path.join(current_dir, input_latex_file_name)
                        self.copy_all(input_latex_file_p, current_dir)  # recurse, might also raise
                        continue

                except InvalidIncludeException as e:
                    Copier._raise_with_info(line_number, latex_file_p, l, e)

    @staticmethod
    def _included_images(l):
        res = []
        for r in _IMAGE_INCLUDE_RE:
            m = r.findall(l)  # regex which has path as 2nd group
            if m:
                res += [p for _, p in m]
        return res

    @staticmethod
    def _included_source_file(current_dir, l):
        # Check if line matches any of the regexes in _RECURSIVE_INCLUDE, return relevant file name
        for regex, filename_group, ext, must_exist in _RECURSIVE_INCLUDE:
            m = regex.search(l)
            if not m:
                continue
            filename = m.group(filename_group)
            _, file_ext = os.path.splitext(filename)
            if file_ext == '':
                filename += ext
            elif file_ext != ext:
                raise InvalidIncludeException('Expected {} file, got {} in {}'.format(ext, filename, l))
            filename = os.path.join(current_dir, filename)
            if not os.path.isfile(filename):
                if not must_exist:
                    return None
                raise InvalidIncludeException('File not found: {} (expected after {})'.format(filename, l))
            return filename
        return None

    @staticmethod
    def _contains_include_statement(l):
        """ Like _included_source_file but only checks for statements, does not evaluate"""
        return any(regex.search(l) for regex, _, _, _ in _RECURSIVE_INCLUDE)

    @staticmethod
    def _raise_with_info(line_number, latex_file_p, line, exception):
        raise ParseLineException('Error on L{} in {} ({}): {}'.format(
                line_number, latex_file_p, line.rstrip(), exception))

    def copy(self, current_dir, p):
        p, ext_in_latex = self.get_actual_p(os.path.join(current_dir, p))
        if p is None:
            return
        out_p = p.replace(current_dir, self.out_dir)
        assert out_p.startswith(self.out_dir)

        if p.endswith('.png') and self.convert_to_jpg:
            p_name = os.path.basename(p)
            assert_exc(not ext_in_latex,
                       '--convert_to_jpg specified but {} is with extension is LaTeX, '
                       'you should remove extension in LaTeX source! I.e. s/{}/{}'.format(
                               p_name, p_name, os.path.splitext(p_name)[0]))
            out_p = out_p.replace('.png', '.jpg')
            os.makedirs(os.path.dirname(out_p), exist_ok=True)
            Image.open(p).save(out_p, quality=95)
            print('Converted!', out_p)
        else:
            try:
                shutil.copy(p, out_p)
            except FileNotFoundError:
                os.makedirs(os.path.dirname(out_p), exist_ok=True)
                shutil.copy(p, out_p)
        self.copied_files.append(out_p)

    @staticmethod
    def get_actual_p(p):
        """ :returns tuple (p', ext_in_latex), where ext_in_latex is a bool indicating whether the extension is
        specified in the latex."""
        if os.path.isfile(p):
            return p, True
        if '#' in p:
            print('Oh no: {}'.format(p))
            return None, False
        candidates = [p + ext for ext in _EXTS if os.path.isfile(p + ext)]
        if len(candidates) == 1:
            return candidates.pop(), False
        if len(candidates) > 1:
            raise FileSearchException('Ambiguous: multiple matches for {}[{}]:\n{}\nRemove unused ones.'.format(
                    p, '|'.join(_EXTS), '\n'.join(candidates)))
        if len(candidates) == 0:
            raise FileSearchException('Did not find file starting with {} and ext in {}'.format(p, _EXTS))


# Strip Comments ---------------------------------------------------------------


def strip_comments_from_line(l, l_prev=None):
    """
    Given a text line l,
    - if it starts with %, return None
    - if it ends with %, return l, it is used for layout!
    - else:
        - find leftmost % that is not \%, delete everything after it
    :return: replaced line, with a trailing \n
    """
    # TODO: keep separator comments or have a flag
    # TODO: currently, TEXT\n%COMMENT\n\n becomes TEXT\n%\n, we only need the % if another paragraph comes.
    #       however, this requires look-ahead
    if l.startswith('%'):  # comment line, replace w/ '%' to keep layout
        # do not print multiple alone % consecutively
        if l_prev == '%\n' or l_prev == '\n':
            # print('HOWDY: >{}< => >{}<'.format(l_prev, l))
            return None
        return '%\n'

    if l.rstrip().endswith('%'):  # layout line, keep
        return l

    leftmost = _get_leftmost_comment(l)
    if not leftmost:  # no % in line, keep
        return l

    # print('stripping >{}<'.format(l.strip()))
    l = l[:leftmost].rstrip() + '\n'
    # print('->        >{}<'.format(l.strip()))
    return l


def _get_leftmost_comment(l):
    leftmost = None
    for i in reversed(range(1, len(l))):
        if l[i] == '%' and l[i-1] != "\\":
            leftmost = i
    return leftmost


def test_strip():
    test_cases = [
        ('asdf\n', 'asdf\n'),
        ('%asdf\n', '%\n'),
        ('layout{%  \n', 'layout{%  \n'),
        ('a%cc\n', 'a\n'),  # one char test
        ('inline comment % starts here % oh another\n', 'inline comment\n'),
        ('percent \\% but then comment % starts here % oh another\n', 'percent \\% but then comment\n'),
        ('percent \\% \\% but then comment % starts here % oh another\n', 'percent \\% \\% but then comment\n')
    ]
    for inp, otp in test_cases:
        assert strip_comments_from_line(inp) == otp


def strip_comments(p):
    """ Remove unneeded comments from LaTeX file `p`. """
    with _modify_file(p) as (fin, fout):
        # strip comments
        l_prev = None
        for l in fin.readlines():
            l = strip_comments_from_line(l, l_prev)
            if not l:
                continue
            l_prev = l
            fout.write(l)
            if '\\end{document}' in l:
                print('Reached {}, stopping...'.format(l.strip()))
                fout.write('\n')
                break


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


def _get_git_hash(p):
    try:
        git_commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=p).decode()
        return git_commit
    except subprocess.CalledProcessError:
        return None


def _insert_in_file(p, text):
    with _modify_file(p) as (fin, fout):
        fout.write(text.rstrip() + '\n' + fin.read())


@contextmanager
def _modify_file(p, out_encoding='utf-8'):
    p_tmp = p + '_tmp'
    assert not os.path.isfile(p_tmp)
    with open(p, 'r') as fin:
        with open(p_tmp, 'w', encoding=out_encoding) as fout:
            yield fin, fout
    os.rename(p_tmp, p)



# Main -------------------------------------------------------------------------


def main(args=sys.argv[1:]):
    p = argparse.ArgumentParser()
    p.add_argument('main_file', help=r'Main Latex file, will copy and also all files included with \input, '
                                     r'\includegraphics, and \overpic. All files in \input are recursively parsed and '
                                     r'treated like the main file. Everything after \\end{document} will be ignored.')
    p.add_argument('other_files', nargs='*', help='Other files not included in the main_file, will copy '
                                                  '(no recursion).')
    p.add_argument('--out_dir', '-o', help='Where to store files.')
    p.add_argument('--rename', '-mv',
                   help='If given, rename OUT_DIR/MAIN_FILE to OUT_DIR/NEW_NAME', metavar='NEW_NAME')
    p.add_argument('--encodings', default=['utf-8'], nargs='+', help='Encodings to try when opening .tex files')
    p.add_argument('--force', '-f', action='store_true', help='If given, delete and re-create OUT_DIR. '
                                                              'WARNING: Calls rm -rf OUT_DIR.')
    p.add_argument('--store_git_hash', '-git', action='store_true',
                   help='If given, add git hash of repo of MAIN_FILE to output file at the top.')
    p.add_argument('--convert_to_jpg', '-jpg', action='store_true', help='If given, convert .pngs to .jpg')
    flags = p.parse_args(args)

    if flags.out_dir is None:
        flags.out_dir = os.path.dirname(flags.main_file) + '_arXiv'

    flags.main_file = os.path.abspath(flags.main_file)
    flags.out_dir = os.path.abspath(flags.out_dir)

    exit_code = copy_latex(flags)
    sys.exit(exit_code)


if __name__ == '__main__':
    main()
