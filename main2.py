"""

How this works:

- Go through file line by line
    - if it's a (re)newcommand: save, move on
    - resolve all (re)newcommands
    - get a list of all images that are referenced
    - get a list of all .tex or .sty files that are referenced -> parse these
    - check for special stuff: bibtex/bibtexstyle

Note: includes are assumed to be relative to root!

"""
import argparse
import glob
import os
import re
import shutil
import sys
from collections import namedtuple
from contextlib import contextmanager
import subprocess

from fjcommon.assertions import assert_exc

# TODO: assumes latexmk exists!
# TODO: renewcommand
# TODO: detect loops in usepackage
# TODO: JPG Conversion is not implemented
# - return (path_in_tex, path_on_disk)  (make sure only one path matching latex on disk)
# - have IMG_EXTS = {...}, STATIC_EXTS = IMG_EXTS | {.pdf}
# - check if path_on_disk in img_exts? -> if -jpg, convert to jpg!


class IncludeCommand(object):
    def __init__(self, regex, path_group, possible_extensions=None, needs_parse=False, must_exist=True):
        """
        :param regex: The regex that matches an include command
        :param path_group: The group of the regex that matches the path of the included file
        Example:
            IncludeCommand(r'\\includegraphics(\[.*\])?{(.*?)}', path_group=2, ...)
            Here, path_group=2 because the first group matches the optional arguments of includegraphics
        """
        if possible_extensions:
            assert all(ext.startswith('.') for ext in possible_extensions), \
                'Must start with dot: {}'.format(possible_extensions)
        self.regex = re.compile(regex)
        self.path_group = path_group
        self.possible_extensions = possible_extensions
        self.needs_parse = needs_parse
        self.must_exist = must_exist


# TODO: rename real_path, it's real_rel or sth!
StaticFile = namedtuple('StaticFile', ['tex_path', 'real_path'])  # real_path is also relative
TexFile = namedtuple('TexFile', ['real_rel_path', 'needs_parse'])  # real_path is also relative


_END_DOCUMENT_MARKER = '\\end{document}'


# We call images or PDFs "static", as they do not need to be parsed.
_EXTS_IMG_CONVERTABLE = {'.jpg'}  # TODO, should be an arg
# _EXTS_IMG = _EXTS_IMG_CONVERTABLE | {'.jpg', '.png', '.jpgs'}
# _EXTS_STATIC = _EXTS_IMG | {'.pdf'}

# TODO: extend with other ways to include images or PDFs.
_STATIC_INCLUDES = [
    IncludeCommand(r'\\includegraphics(\[.*?\])?{(.*?)}', 2),
    IncludeCommand(r'\\overpic(\[.*?\])?{(.*?)}', 2)
]


_TEX_INCLUDES = [
    IncludeCommand(r'\\input{(.*?)}', 1, {'.tex'}, needs_parse=True),
    IncludeCommand(r'\\subfile{(.*?)}', 1, {'.tex'}, needs_parse=True),
    IncludeCommand(r'\\usepackage(\[.*?\])?{(.*?)}', 2, {'.sty'}, needs_parse=True, must_exist=False),
    IncludeCommand(r'\\bibliographystyle{(.*?)}', 1, {'.bst'}, needs_parse=False),
    IncludeCommand(r'\\bibliography{(.*?)}', 1, {'.bib'}, needs_parse=False),
]

_RE_NEWCOMMAND = re.compile(r'\\(re)?newcommand\*?{?(.*?)}?(\[(\d+)\])?{')


class ParseException(Exception):
    pass


def copy_latex(flags):
    """Main function."""
    c = Copier(flags.encodings, flags.main_file, flags.out_dir)
    main_file_out = c.copy(flags.store_git_hash, flags.rename)
    sizes = c.copied_file_sizes()
    print('Biggest files:')
    print('\n'.join('{}kB: {}'.format(s, p) for s, p in sorted(sizes, reverse=True)[:10]))
    print('Total: {}kB'.format(sum(s for s, _ in sizes)))

    _compile_and_keep_bbl(main_file_out)
    # TODO(release): must compile first and get .bbl
    tar_file_name = os.path.splitext(os.path.basename(main_file_out))[0] + '.tar'
    subprocess.call(f'tar -cvf ../{tar_file_name} *', shell=True, cwd=flags.out_dir)
    tar_out_dir = os.path.abspath(os.path.join(flags.out_dir, '..'))
    print(f'DONE! Upload {tar_file_name} (stored in {tar_out_dir}).')


def _compile_and_keep_bbl(main_file_out):
    print('*** Compiling', main_file_out)
    out_dir = os.path.dirname(main_file_out)
    files_before_compile = set(os.listdir(out_dir))
    assert not any(p.endswith('.bbl') for p in files_before_compile)
    _compile(main_file_out)
    files_after_compile = set(os.listdir(out_dir))
    print(f'*** Searching for .bll file in {files_after_compile}...')
    try:
        bbl_file = next(p for p in files_after_compile if p.endswith('.bbl'))
    except StopIteration:
        print('*** Error! .bbl file not found. Did you compile?')
        sys.exit(1)
    pdf_out = next(p for p in files_after_compile if p.endswith('.pdf'))
    os.rename(os.path.join(out_dir, pdf_out), os.path.abspath(os.path.join(out_dir, '..', pdf_out)))
    print('Keeping', pdf_out, '-- please check!')
    unneeded_files = (files_after_compile - files_before_compile) - {bbl_file, pdf_out}
    print('Unneeded', unneeded_files)
    for unneeded_file in unneeded_files:
        p = os.path.join(out_dir, unneeded_file)
        os.remove(p)


def _compile(main_file_out):
    cwd, filename = os.path.split(main_file_out)
    assert filename.endswith('.tex'), filename
    cmd = ['latexmk', filename, '--view=pdf']
    try:
        subprocess.call(cmd, cwd=cwd, stdout=subprocess.DEVNULL)
    except FileNotFoundError:
        cmd = ' '.join(cmd)
        print('*** Error when running `{}` in {}'.format(cmd, cwd))
        print('*** Please run a compile step in another shell and return here.')
        out = input('*** Type "yes" if ready >> ')
        if out != 'yes':
            print('*** Abort')
            sys.exit(1)


def _replace_all(s, rep):
    assert all(isinstance(v, str) for v in rep.values())
    rep = {re.escape(k): v for k, v in rep.items()}
    pattern = re.compile('|'.join(rep.keys()))
    return pattern.sub(lambda m: rep[re.escape(m.group(0))], s)


def _iter_lines(first_line, f_iter):
    yield first_line
    for _, other_line in f_iter:
        yield other_line


def test_consumme():
    first_line = '{hithere \\textbf{hi} \\textbf{oh\n'
    f_iter = enumerate(['foo} % something in a bracket\n', 'final} some more text \n'])
    consummed = _consume_until_closing_bracket(first_line, f_iter)
    assert consummed == ('hithere \\textbf{hi} \\textbf{oh\nfoo}\nfinal', ' some more text \n')


def _consume_until_closing_bracket(first_line, f_iter):
    """
    Given a line `first_line`, starting with a opening bracket, and a file iterator `f_iter`, consume characters from
    `first_line` and potentially `f_iter` until the closing bracket matching first_line[0]
    Example
        first_line = '{start \textbf{hi\n'
        f_iter = enumerate([ 'more lines} % comment {}\n',
                             'finishing}\n'
    -> return
        'start \textbf{hi\nmore lines}\nfinishing'
    :return: the complete command started, without the surronding bracekts, as well as the remaining text of the
    final consummed line for further parsing.
    """
    assert first_line[0] == '{'
    assert first_line[-1] == '\n'
    consummed = ''  # all consummed characters
    num_brackets = 0  # number of opened brackets
    for num_lines, line in enumerate(_iter_lines(first_line, f_iter)):
        if num_lines > 100:
            raise ValueError('Could not find closing brackets...')
        line = strip_comments_from_line(line)
        for i, c in enumerate(line):
            if c == '{':
                num_brackets += 1
            elif c == '}':
                num_brackets -= 1
            if num_brackets == 0:
                remaining_line = line[i+1:]
                # remove initial {
                return consummed[1:], remaining_line
            consummed += c
    raise ValueError('Could not find needed closing brackets!')


class Copier(object):
    def __init__(self, encodings, tex_root_file, out_dir):
        self.encodings = encodings
        self.tex_root_dir = os.path.dirname(os.path.abspath(tex_root_file))
        # Relative to tex_root_dir.
        self.tex_root_p = os.path.basename(tex_root_file)
        assert os.path.isfile(os.path.join(self.tex_root_dir, self.tex_root_p))

        self.out_dir = os.path.abspath(out_dir)

        self._convert_jpg_exts = []  # [] if not set!
        self._copied_file_ps = set()

        # _regexes: dictionary {\command -> compiled regexes matching command invocations}
        # _command_definitions: dictionary {\command -> (definition, num_args)
        # Example:
        #   regexes = {r"\imgs":      r'(\\imgs){(.*?)}{(.*?)}',
        #              r"\imagesdir": r'(\\imagesdir){(.*?)}',
        #              r"\noargs":    r'(\\noargs)(\W|$)' }
        #   definitions = {r"\imgs":      (r"\include[123]{\imagesdir{2}/#1/#2.jpg}", 2),
        #                  r"\imagesdir": ("imgs_#1", 1),
        #                  r"\noargs":    ("Using \imgs{hello}{world}", 0)}
        self._regexes = {}
        self._command_definitions = {}

    def copy(self, store_git_hash=False, rename=None):
        """Copy main file recursively."""
        self._copy(self.tex_root_p)  # TODO: maybe copy and strip
        self._parse_file(self.tex_root_p)
        main_file_out = os.path.join(self.out_dir, self.tex_root_p)
        if store_git_hash:
            self._store_git_hash(main_file_out)
        if rename:
            _, ext = os.path.splitext(rename)
            if not ext:
                rename += '.tex'
            main_file_out_new = os.path.join(self.out_dir, rename)
            os.rename(main_file_out, main_file_out_new)
            self._copied_file_ps.remove(main_file_out)
            self._copied_file_ps.add(main_file_out_new)
            main_file_out = main_file_out_new
        return main_file_out

    def _store_git_hash(self, main_file_out):
        git_hash = self._get_git_hash()
        if git_hash:
            print('Writing git hash {}...'.format(git_hash))
            _insert_in_file(main_file_out, text='% ' + git_hash)

    def _get_git_hash(self):
        repo = self.tex_root_dir
        try:
            git_commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo).decode()
            return git_commit.strip()
        except subprocess.CalledProcessError:
            return None

    def copied_file_sizes(self):
        return [(os.path.getsize(p) // 1028, p) for p in self._copied_file_ps]

    def _parse_file(self, relative_p):
        # if '.sty' in relative_p:
        #     print('Skipping', relative_p)
        #     return
        p = os.path.join(self.tex_root_dir, relative_p)
        is_sty_file = relative_p.endswith('.sty')
        print(f'Parsing {p}, is_sty_file={is_sty_file}...')
        with _open(p, self.encodings) as f:
            f_iter = enumerate(iter(f))
            for i, line in f_iter:
                if _END_DOCUMENT_MARKER in line:
                    print(f'*** Found `{line.strip()}`, stopping parsing!')
                    break
                # To make sure we do not parse anything commented out.
                # We strip the comments again after copying.
                line = strip_comments_from_line(line)
                if not is_sty_file:
                    line = self._extract_definition(line, f_iter)
                    line = self._resolve_definitions(line)
                # note that at this point, l might be multiple lines due to resolving some definition
                for tex_file in self._included_tex_files(line):
                    self._copy(tex_file.real_rel_path)
                    if tex_file.needs_parse:  # false for .bst, .bib files
                        self._parse_file(tex_file.real_rel_path)
                for static_file in self._included_static_files(line):
                    self._copy_static(static_file)

    def _extract_definition(self, line, f_iter):
        m = _RE_NEWCOMMAND.search(line)
        if not m:
            return line
        # Assume you are given a valid latex line. Since \newcommand can appear anywhere, there might be brackets
        # unrelated to the \newcommand. (e.g. foo} bar \newcommand{\foo}{bar} text)
        #  -> return everything starting from the defining bracket (e.g. {bar})
        # the regex ends at the starting bracket of the definition
        line = line[m.end() - 1:]
        command, remaining_line = _consume_until_closing_bracket(line, f_iter)
        # _RE_NEWCOMMAND = \\(re)?newcommand{?(.*?)}?(\[(\d+)\])?{'
        # Groups:            1                2         4
        # Extract:
        is_renew, command_name, num_args = m.group(1) is not None, m.group(2), m.group(4)

        if command_name in self._regexes:
            # This is a LaTeX syntax error but detecting it here anyway.
            if not is_renew:
                raise ParseException('Redefining {}'.format(command_name))
            # remove previous
            del self._regexes[command_name]
            del self._command_definitions[command_name]

        if num_args is None:
            num_args = 0
            # TODO: match more stuff after command, e.g. end of string?
            regex = '(\\' + command_name + ')(\W|$)'  # escape the initial backslash of `command_name`
        else:
            num_args = int(num_args)
            regex = '(\\' + command_name + ')' + r'{(.*?)}' * num_args

        print(f'--- Compilinig {command_name}: {regex}; Command:\n{command}\n---')
        self._regexes[command_name] = re.compile(regex)
        self._command_definitions[command_name] = (command, num_args)

        return remaining_line

    def _copy(self, relative_p):
        """Copy file at `relative_p` to output. If .tex file, strip comments."""
        print('Copying', relative_p, '...')
        p = os.path.join(self.tex_root_dir, relative_p)
        assert os.path.isfile(p), f'Expected file at {p} (make sure this is not a directory).'

        outp = os.path.join(self.out_dir, relative_p)
        shutil.copy(p, outp)
        self._copied_file_ps.add(outp)

        if outp.endswith('.tex'):
            strip_comments(outp)

    def _copy_static(self, static_file: StaticFile):
        """copy static file (images, pdfs, etc.)

        Compresses also!
        :param static_file:
        :return:
        """
        print('*** static', static_file)
        p = os.path.join(self.tex_root_dir, static_file.real_path)
        out_p = os.path.join(self.out_dir, static_file.real_path)
        os.makedirs(os.path.dirname(out_p), exist_ok=True)  # real_path might contain a dir, e.g., img/A.png
        _, real_ext = os.path.splitext(static_file.real_path)
        if real_ext not in self._convert_jpg_exts:
            print('*** static -> cp', p, out_p)
            shutil.copy(p, out_p)
            self._copied_file_ps.add(out_p)
            return
        _, tex_ext = os.path.splitext(static_file.tex_path)
        if tex_ext != '' and tex_ext != real_ext:
            # If the LaTeX source contains imgA.png and we save it as imgA.jpg, there will be a compile error.
            # This is fixed by changing source to imgA only, and let latex figure add the extension.
            # Note that `_real_path_for_static_file` already makes sure that there is only one match for
            # tex_path*, so removing the extension should always be safe at this point.
            tex_path = static_file.tex_path
            raise ParseException(
                    'Cannot convert {} to .jpg, since it is used with extension in LaTeX source! '
                    'Please replace {} with {} and try again.'.format(
                            tex_path, tex_path, os.path.splitext(tex_path)[0]))
        new_out_p = os.path.splitext(out_p)[0] + '.jpg'
        self._save_as_jpg(p, new_out_p)  # TODO: not implemented!

    def _resolve_definitions(self, s):
        """
        :param s: string to replace in
        :return: s with every used definition replaced
        """
        # replacement function used for re.sub, mapping regex match to string
        repl = self._replace_defs_for_match
        for r in self._regexes.values():
            s = r.sub(repl, s)
        return s

    def _replace_defs_for_match(self, match):
        command = match.group(1)
        definition, num_args = self._command_definitions[command]
        if num_args > 0:
            # regex is (\command){(.*?)}{(.*?)}{...} -> groups 1 to end are arguments to \command
            args = match.groups()[1:]
            # This would actually be a LaTeX syntax error, so is not really expected.
            assert_exc(
                    len(args) == num_args,
                    'Expected {} to be invoced with {} arguments, got {}'.format(command, num_args, match.group()),
                    ParseException)
            # Replace #1, #2, #3 in the command definition with the actual arguments provided
            replacements = {'#' + str(i+1): arg for i, arg in enumerate(args)}
            activated_definition = _replace_all(definition, replacements)
        else:
            # regex is (\command)(\W|$), where the (\W|$) matches the non-word character following \command.
            # Note sure how conformant this is with LaTeX syntax.
            activated_definition = definition + match.group(2)
        # recursion: make sure any definitions used within definitions are covered
        return self._resolve_definitions(activated_definition)

    def _included_tex_files(self, l):
        for m, include_command in Copier._match_all(l, _TEX_INCLUDES):
            tex_path = m.group(include_command.path_group)
            real_rel_path = self._real_rel_path_for_tex_file(
                    tex_path, include_command.possible_extensions, include_command.must_exist)
            if real_rel_path:
                yield TexFile(real_rel_path, include_command.needs_parse)

    def _included_static_files(self, l):
        for m, include_command in Copier._match_all(l, _STATIC_INCLUDES):
            tex_path = m.group(include_command.path_group)
            print('***', tex_path)
            # this is actually a full fucking path
            real_path = self._real_path_for_static_file(tex_path)
            rel_path = real_path.replace(self.tex_root_dir, '').lstrip(os.path.sep)
            yield StaticFile(tex_path, rel_path)

    # TODO: rename
    def _real_path_for_static_file(self, tex_path):
        real_path = os.path.join(self.tex_root_dir, tex_path)
        _, ext = os.path.splitext(tex_path)
        if ext != '':
            if not os.path.isfile(real_path):
                raise ParseException('File {} does not exist!'.format(real_path))
            return real_path
        candidates = glob.glob(real_path + '.*')
        # ==0 should not happen for a valid LaTeX
        # >1  can happen, but we do not handle it for now
        if len(candidates) != 1:
            raise ParseException(
                    'Expected exactly 1 file matching {}, got: {} (Files without extension are not supported'.format(
                            real_path + '.*', candidates or 'None'))
        return candidates.pop()

    def _real_rel_path_for_tex_file(self, tex_path, possible_extensions, must_exist):
        real_path = os.path.join(self.tex_root_dir, tex_path)
        _, ext = os.path.splitext(tex_path)
        if ext != '':
            if not os.path.isfile(real_path) and must_exist:
                raise ParseException('File {} does not exist!'.format(real_path))
            return tex_path
        for possible_extension in possible_extensions:
            if os.path.isfile(real_path + possible_extension):
                return tex_path + possible_extension
        if must_exist:
            raise ParseException(
                    'Expected to find file in {} starting with {} and ending with {}. Consider renaming file to match '
                    'expected extension OR filing a bug report / updating the expected extensions.'.format(
                            self.tex_root_dir, tex_path, '|'.join(possible_extensions)))

    @staticmethod
    def _match_all(l, include_commands):
        for include_command in include_commands:
            for m in include_command.regex.finditer(l):
                yield m, include_command


def _note_on_extensions(real_path, expected_extensions):
    pass


def test_dirs():
    c = Copier(['utf-8'], '/Hello/Word/main.tex', '/Users/out')
    assert c.tex_root_dir == '/Hello/Word'
    c = Copier(['utf-8'], 'main.tex', '/Users/out')
    assert c.tex_root_dir == os.getcwd()


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
    if l.lstrip().startswith('%'):  # comment line, replace w/ '%' to keep layout
        # do not print multiple alone % consecutively
        if l_prev == '%\n' or l_prev == '\n':
            return None
        return '%\n'

    if l.rstrip().endswith('%'):  # layout line, keep
        return l

    leftmost = _get_leftmost_comment(l)
    if not leftmost:  # no % in line, keep
        return l

    l = l[:leftmost].rstrip() + '\n'
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


# TODO
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
            if _END_DOCUMENT_MARKER in l:
                print('Reached {}, stopping...'.format(l.strip()))
                fout.write('\n')
                break


@contextmanager
def _open(p, encodings):
    for enc in encodings:
        try:
            f = open(p, 'r', encoding=enc)
            yield f
            f.close()
            break
        except UnicodeDecodeError as e:
            print('Error while reading {} with {}: {}'.format(p, enc, e))


@contextmanager
def _modify_file(p, out_encoding='utf-8'):
    p_tmp = p + '_tmp'
    assert not os.path.isfile(p_tmp)
    with open(p, 'r') as fin:
        with open(p_tmp, 'w', encoding=out_encoding) as fout:
            yield fin, fout
    os.rename(p_tmp, p)



def _insert_in_file(p, text):
    with _modify_file(p) as (fin, fout):
        fout.write(text.rstrip() + '\n' + fin.read())


def main(args=sys.argv[1:]):
    p = argparse.ArgumentParser()
    p.add_argument('main_file')
    p.add_argument('--out_dir', '-o', help='Where to store files. By default, create a directory above input.')
    p.add_argument('--encodings', default=['utf-8'], nargs='+', help='Encodings to try when opening .tex files')
    p.add_argument('--force', '-f', action='store_true', help='If given, delete and re-create OUT_DIR. '
                                                              'WARNING: Calls rm -rf OUT_DIR.')
    p.add_argument('--store_git_hash', '-git', action='store_true',
                   help='If given, add git hash of repo of MAIN_FILE to output file at the top.')
    # TODO: not implemented??
    p.add_argument('--convert_to_jpg', '-jpg', action='store_true',
                   help='If given, convert .pngs to .jpg. NOT YET IMPLEMENTED!')

    p.add_argument('--rename', '-mv',
                   help='If given, rename OUT_DIR/MAIN_FILE to OUT_DIR/NEW_NAME', metavar='NEW_NAME')
    flags = p.parse_args(args)

    if flags.out_dir is None:
        flags.out_dir = os.path.dirname(os.path.abspath(flags.main_file)) + '_arXivout'

    if os.path.isdir(flags.out_dir):
        print(f'*** OUT_DIR={flags.out_dir} exists! Delete or pass -f.')
        if not flags.force:
            sys.exit(1)
        print('*** Removing...')
        shutil.rmtree(flags.out_dir)
    os.makedirs(flags.out_dir, exist_ok=False)

    copy_latex(flags)



if __name__ == '__main__':
    main()