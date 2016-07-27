"""
This file contains a management command for exporting the modulestore to
neo4j, a graph database.
"""
import logging

from django.conf import settings
from django.core.management.base import BaseCommand
from py2neo import Graph, Node, Relationship
from py2neo.compat import integer, string, unicode as neo4j_unicode
from request_cache.middleware import RequestCache
from xmodule.modulestore.django import modulestore

log = logging.getLogger(__name__)

bolt_log = logging.getLogger('neo4j.bolt')  # pylint: disable=invalid-name
bolt_log.propagate = False
bolt_log.disabled = True

ITERABLE_NEO4J_TYPES = (tuple, list, set, frozenset)
PRIMITIVE_NEO4J_TYPES = (integer, string, neo4j_unicode, float, bool)


class ModuleStoreSerializer(object):
    """
    Class with functionality to serialize a modulestore into subgraphs,
    one graph per course.
    """
    def __init__(self):
        self.all_courses = modulestore().get_course_summaries()

    @staticmethod
    def serialize_item(item):
        """
        Args:
            item: an XBlock

        Returns:
            fields: a dictionary of an XBlock's field names and values
            label: the name of the XBlock's type (i.e. 'course'
            or 'problem')
        """
        # convert all fields to a dict and filter out parent and children field
        fields = dict(
            (field, field_value.read_from(item))
            for (field, field_value) in item.fields.iteritems()
            if field not in ['parent', 'children']
        )

        course_key = item.scope_ids.usage_id.course_key

        # set or reset some defaults
        fields['edited_on'] = unicode(getattr(item, 'edited_on', u''))
        fields['display_name'] = item.display_name_with_default
        fields['org'] = course_key.org
        fields['course'] = course_key.course
        fields['run'] = course_key.run
        fields['course_key'] = unicode(course_key)

        label = item.scope_ids.block_type

        # prune some fields
        if label == 'course':
            if 'checklists' in fields:
                del fields['checklists']

        return fields, label

    def serialize_course(self, course_id):
        """
        Args:
            course_id: CourseKey of the course we want to serialize

        Returns:
            nodes: a list of py2neo Node objects
            relationships: a list of py2neo Relationships objects

        Serializes a course into Nodes and Relationships
        """
        # create a location to node mapping we'll need later for
        # writing relationships
        location_to_node = {}
        items = modulestore().get_items(course_id)

        # create nodes
        nodes = []
        for item in items:
            fields, label = self.serialize_item(item)

            for field_name, value in fields.iteritems():
                fields[field_name] = self.coerce_types(value)

            node = Node(label, **fields)
            nodes.append(node)
            location_to_node[item.location] = node

        # create relationships
        relationships = []
        for item in items:
            for child_loc in item.get_children():
                parent_node = location_to_node.get(item.location)
                child_node = location_to_node.get(child_loc.location)
                if parent_node is not None and child_node is not None:
                    relationship = Relationship(parent_node, "PARENT_OF", child_node)
                    relationships.append(relationship)

        return nodes, relationships

    @staticmethod
    def coerce_types(value):
        """
        Args:
            value: the value of an xblock's field

        Returns: either the value, a unicode version of the value, or, if the
        value is iterable, the value with each element being converted to unicode
        """
        coerced_value = value
        if isinstance(value, ITERABLE_NEO4J_TYPES):
            coerced_value = []
            for element in value:
                coerced_value.append(unicode(element))
            # convert coerced_value back to its original type
            coerced_value = type(value)(coerced_value)

        # if it's not one of the types that neo4j accepts,
        # just convert it to unicode
        elif not isinstance(value, PRIMITIVE_NEO4J_TYPES):
            coerced_value = unicode(value)

        return coerced_value


class Command(BaseCommand):
    """
    Command to dump modulestore data to neo4j
    """
    @staticmethod
    def add_to_transaction(neo4j_entities, transaction):
        """
        Args:
            neo4j_entities: a list of Nodes or Relationships
            transaction: a neo4j transaction
        """
        for entity in neo4j_entities:
            transaction.create(entity)

    def handle(self, *args, **options):  # pylint: disable=unused-argument
        """
        Iterates through each course, serializes them into graphs, and saves
        those graphs to neo4j.
        """
        mss = ModuleStoreSerializer()
        graph = Graph(**settings.NEO4J_CONFIG)

        log.info("deleting existing coursegraph data")
        graph.delete_all()
        total_number_of_courses = len(mss.all_courses)

        for index, course in enumerate(mss.all_courses):
            # first, clear the request cache to prevent memory leaks
            RequestCache.clear_request_cache()

            log.info(
                u"Now exporting %s to neo4j: course %d of %d total courses",
                course.id,
                index + 1,
                total_number_of_courses
            )
            nodes, relationships = mss.serialize_course(course.id)

            transaction = graph.begin()

            try:
                self.add_to_transaction(nodes, transaction)
                self.add_to_transaction(relationships, transaction)
                transaction.commit()

            except Exception:  # pylint: disable=broad-except
                log.exception(
                    u"Error trying to dump course %s to neo4j, rolling back",
                    unicode(course.id)
                )
                transaction.rollback()
