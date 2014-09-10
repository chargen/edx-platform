import json
import random
import logging
from collections import OrderedDict

from xmodule.x_module import XModule
from xmodule.seq_module import SequenceDescriptor

from lxml import etree

from xblock.fields import Scope, String, List
from xblock.fragment import Fragment

log = logging.getLogger('edx.' + __name__)


class RandomizeFields(object):
    choice = String(help="Which random child was chosen",
                    scope=Scope.user_state)
    history = List(help="History of randomize choices", scope=Scope.user_state)
    suspended = List(help="Suspended problems", scope=Scope.settings)


class RandomizeModule(RandomizeFields, XModule):
    """
    Chooses a random child module. Chooses the same one every time for each
    student.

     Example:
     <randomize>
     <problem url_name="problem1" />
     <problem url_name="problem2" />
     <problem url_name="problem3" />
     </randomize>

    User notes:
      - If you're randomizing amongst graded modules, each of them MUST be
        worth the same number of points. Otherwise, the earth will be overrun
        by monsters from the deeps. You have been warned.

    Technical notes:
      - There is more dark magic in this code than I'd like. The whole
        varying-children + grading interaction is a tangle between super and
        subclasses of descriptors and modules.
    """
    def __init__(self, *args, **kwargs):
        super(RandomizeModule, self).__init__(*args, **kwargs)
        # NOTE: calling self.get_children() creates a circular reference --
        # it calls get_child_descriptors() internally, but that doesn't work
        # until we've picked a choice
        xml_attrs = self.descriptor.xml_attributes or []
        self.use_randrange = self._str_to_bool(xml_attrs.get('use_randrange', ''))
        self.no_repeats = self._str_to_bool(xml_attrs.get('no_repeats', ''))
        self.pick_choice(use_randrange=self.use_randrange,
                         no_repeats=self.no_repeats, suspended=self.suspended)

    def _str_to_bool(self, v):
        return v.lower() == 'true'

    def pick_choice(self, use_randrange=False, no_repeats=False,
                    suspended=None):
        choices = self.get_choices(no_repeats=no_repeats, suspended=suspended)
        all_choices = self.get_choices()
        current_child = all_choices.get(self.choice)
        if current_child and self.choice not in choices:
            if current_child.location.name not in suspended:
                # Children changed. Reset.
                self.choice = None

        if self.choice is None:
            num_choices = len(choices)
            # choose one based on the system seed, or randomly if that's not
            # available
            if num_choices > 0:
                keys = choices.keys()
                if self.system.seed is not None and not use_randrange:
                    choice = keys[self.system.seed % num_choices]
                    log.debug('using seed for %s choice=%s' %
                              (str(self.location), self.choice))
                else:
                    choice = random.choice(keys)
                    log.debug('using randrange for %s' % str(self.location))
                self.choice = choice
            else:
                # Student has seen all problems. Ideally we autogenerate a fake
                # CapaModule problem and leave it until history gets wiped by
                # an instructor/TA but for now we use the last seen choice from
                # history.
                log.error("No choices left!!! Attempting to use last "
                          "item in history")
                try:
                    self.choice = self.history[-1]
                except IndexError:
                    log.error("Failed to load last item in history")

        if self.choice is not None:
            self.child_descriptor = all_choices.get(self.choice)
            # Now get_children() should return a list with one element
            children = self.get_children()
            if len(children) != 1:
                raise Exception("self.get_children() returned %d elements:\n%s"
                                % (len(children), str(children)))
            self.child = children[0]
        else:
            self.child_descriptor = None
            self.child = None

    def get_choices(self, no_repeats=None, suspended=None):
        children = self.descriptor.get_children()
        if self.choice is None and no_repeats:
            children = [c for c in children if c.location.url() not in
                        self.history]
        if suspended:
            children = [c for c in children if c.location.name not in
                        suspended]
        return OrderedDict([(c.location.url(), c) for c in children])

    def get_choice_index(self, choice=None, choices=None):
        choice = choice or self.choice
        choices = choices or self.get_choices().keys()
        return choices.index(choice)

    def get_child_descriptors(self):
        """
        For grading--return just the chosen child.
        """
        if self.child_descriptor is None:
            return []
        return [self.child_descriptor]

    def student_view(self, context):
        if self.child is None:
            # raise error instead?  In fact, could complain on descriptor load
            return Fragment(content=u"<div>Nothing to randomize between</div>")
        else:
            child_loc = self.child.location.url()
            if child_loc not in self.history:
                self.history.append(child_loc)
            self.save()
        child_html = self.child.render('student_view', context)
        if self.system.user_is_staff:
            choices = self.get_choices().keys()
            dishtml = self.system.render_template('randomize_control.html', {
                'element_id': self.location.html_id(),
                'is_staff': self.system.user_is_staff,
                'ajax_url': self.system.ajax_url,
                'choice': self.get_choice_index(choices=choices),
                'num_choices': len(choices),
            })
            # html = '<html><p>Welcome, staff.  Randomize loc=%s ;
            # Choice=%s</p><br/><hr/></br/>' % (str(self.location),
            # self.choice)
            return Fragment(
                u"<html>" + dishtml + child_html.content + u"</html>")
        return child_html

    def handle_ajax(self, dispatch, data):
        if self.runtime.user_is_staff:
            choices = self.get_choices().keys()
            index = self.get_choice_index(choices=choices)
            try:
                if dispatch == 'next':
                    self.choice = choices[index + 1]
                elif dispatch == 'jump':
                    log.debug('jump, data=%s' % data)
                    self.choice = choices[int(data['choice'])]
            except (IndexError, ValueError):
                # log the error in this case and return the current choice
                log.exception("error in randomize next/jump (IGNORED):")
        result = {'ret': "choice=%s" % self.choice}
        return json.dumps(result)

    def get_icon_class(self):
        return self.child.get_icon_class() if self.child else 'other'


class RandomizeDescriptor(RandomizeFields, SequenceDescriptor):
    # the editing interface can be the same as for sequences -- just a
    # container
    module_class = RandomizeModule
    filename_extension = "xml"

    def definition_to_xml(self, resource_fs):
        xml_object = etree.Element('randomize')
        for child in self.get_children():
            self.runtime.add_block_as_child_node(child, xml_object)
        return xml_object

    def has_dynamic_children(self):
        """
        Grading needs to know that only one of the children is actually "real".
        This makes it use module.get_child_descriptors().
        """
        return True
