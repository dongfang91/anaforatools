__author__ = 'bethard'

import argparse
import collections
import glob
import logging
import os
import re

import anafora
import anafora.validate


class Scores(object):
    def __init__(self):
        self.reference = 0
        self.predicted = 0
        self.correct = 0

    def add(self, reference, predicted):
        """
        :param set reference: the reference annotations
        :param set predicted: the predicted annotations
        :return tuple: (annotations only in reference, annotations only predicted)
        """
        self.reference += len(reference)
        self.predicted += len(predicted)
        self.correct += len(reference & predicted)
        return reference - predicted, predicted - reference

    def update(self, other):
        self.reference += other.reference
        self.predicted += other.predicted
        self.correct += other.correct

    def precision(self):
        return 1.0 if self.predicted == 0 else self.correct / float(self.predicted)

    def recall(self):
        return 1.0 if self.reference == 0 else self.correct / float(self.reference)

    def f1(self):
        p = self.precision()
        r = self.recall()
        return 0.0 if p + r == 0.0 else 2 * p * r / (p + r)

    def __repr__(self):
        return "{0}(reference={1}, predicted={2}, correct={3})".format(
            self.__class__.__name__, self.reference, self.predicted, self.correct
        )


class _OverlappingWrapper(object):
    def __init__(self, annotation, seen=None):
        self.annotation = annotation
        self.type = self.annotation.type
        self.parents_type = self.annotation.parents_type
        if isinstance(annotation, anafora.AnaforaEntity):
            self.spans = _OverlappingSpans(self.annotation.spans)
        if isinstance(annotation, anafora.AnaforaRelation):
            self.spans = tuple(map(_OverlappingSpans, annotation.spans))
        if seen is None:
            seen = set()
        self.properties = {}
        for name, value in self.annotation.properties.items():
            if id(value) not in seen:
                seen.add(id(value))
                if isinstance(value, anafora.AnaforaAnnotation):
                    self.properties[name] = _OverlappingWrapper(value, seen)
                else:
                    self.properties[name] = value

    def _key(self):
        return self.spans, self.type, self.parents_type, self.properties

    def __eq__(self, other):
        return self._key() == other._key()

    def __hash__(self):
        return hash(anafora._to_frozensets(self))

    def __repr__(self):
        return "{0}({1})".format(self.__class__.__name__, self.annotation)


class _OverlappingSpans(object):
    def __init__(self, spans):
        self.spans = spans

    def __eq__(self, other):
        for self_start, self_end in self.spans:
            for other_start, other_end in other.spans:
                if self_start < other_end and other_start < self_end:
                    return True
        return False

    def __hash__(self):
        return 0

    def __repr__(self):
        return "{0}({1})".format(self.__class__.__name__, self.spans)


def _group_by(reference_iterable, predicted_iterable, key_function):
    result = collections.defaultdict(lambda: (set(), set()))
    for iterable, index in [(reference_iterable, 0), (predicted_iterable, 1)]:
        for item in iterable:
            result[key_function(item)][index].add(item)
    return result


def score_data(reference_data, predicted_data, include=None, exclude=None, annotation_wrapper=None, file_name=None):
    """
    :param AnaforaData reference_data: reference ("gold standard") Anafora data
    :param AnaforaData predicted_data: predicted (system-generated) Anafora data
    :param set include: types of annotations to include (others will be excluded); may be type names,
        (type-name, property-name) tuples, (type-name, property-name, property-value) tuples
    :param set exclude: types of annotations to exclude; may be type names, (type-name, property-name) tuples,
        (type-name, property-name, property-value) tuples
    :param type annotation_wrapper: wrapper object to apply to AnaforaAnnotations
    :param string file_name: name of the text file being compared (used only for logging purposes)
    :return dict: mapping of (annotation type, property) to Scores object
    """
    def _accept(type_name, prop_name=None, prop_value=None):
        if include is not None:
            if type_name not in include:
                if (type_name, prop_name) not in include:
                    if (type_name, prop_name, prop_value) not in include:
                        return False
        if exclude is not None:
            if type_name in exclude:
                return False
            if (type_name, prop_name) in exclude:
                return False
            if (type_name, prop_name, prop_value) in exclude:
                return False
        return True

    def _props(annotations):
        props = set()
        for ann in annotations:
            spans = ann.spans
            if _accept(ann.type, "<span>"):
                props.add((spans, (ann.type, "<span>")))
            for prop_name in ann.properties:
                prop_value = ann.properties[prop_name]
                if _accept(ann.type, prop_name):
                    props.add((spans, (ann.type, prop_name), prop_value))
                if _accept(ann.type, prop_name, prop_value) and isinstance(prop_value, basestring):
                    props.add((spans, (ann.type, prop_name, prop_value), prop_value))
        return props

    result = collections.defaultdict(lambda: Scores())
    reference_annotations = reference_data.annotations
    predicted_annotations = [] if predicted_data is None else predicted_data.annotations
    if annotation_wrapper is not None:
        reference_annotations = map(annotation_wrapper, reference_annotations)
        predicted_annotations = map(annotation_wrapper, predicted_annotations)
    groups = _group_by(reference_annotations, predicted_annotations, lambda a: a.type)
    for ann_type in sorted(groups):
        reference_annotations, predicted_annotations = groups[ann_type]
        if _accept(ann_type):
            missed, added = result[ann_type].add(reference_annotations, predicted_annotations)
            if predicted_data is not None:
                for annotation in missed:
                    logging.debug("Missed%s:\n%s", " in " + file_name if file_name else "", str(annotation).rstrip())
                for annotation in added:
                    logging.debug("Added%s:\n%s", " in " + file_name if file_name else "", str(annotation).rstrip())

        prop_groups = _group_by(_props(reference_annotations), _props(predicted_annotations), lambda t: t[1])
        for name in sorted(prop_groups):
            reference_tuples, predicted_tuples = prop_groups[name]
            result[name].add(reference_tuples, predicted_tuples)

    return result


def _load_and_remove_errors(schema, xml_path):
    if not os.path.exists(xml_path):
        logging.warn("%s: no such file", xml_path)
        return None
    try:
        data = anafora.AnaforaData.from_file(xml_path)
    except anafora.ElementTree.ParseError:
        logging.warn("%s: ignoring invalid XML", xml_path)
        return None
    else:
        errors = schema.errors(data)
        while errors:
            for annotation, error in errors:
                logging.warn("%s: removing invalid annotation: %s", xml_path, error)
                data.annotations.remove(annotation)
            errors = schema.errors(data)
        return data


def score_dirs(schema, reference_dir, predicted_dir, include=None, exclude=None, annotation_wrapper=None):
    """
    :param schema: Anafora schema against which Anafora XML should be valdiated
    :param string reference_dir: directory containing reference ("gold standard") Anafora XML directories
    :param string predicted_dir: directory containing predicted (system-generated) Anafora XML directories
    :param set include: types of annotations to include (others will be excluded); may be type names,
        (type-name, property-name) tuples, (type-name, property-name, property-value) tuples
    :param set exclude: types of annotations to exclude; may be type names, (type-name, property-name) tuples,
        (type-name, property-name, property-value) tuples
    :param type annotation_wrapper: wrapper object to apply to AnaforaAnnotations
    :return dict: mapping of (annotation type, property) to Scores object
    """
    result = collections.defaultdict(lambda: Scores())

    for _, sub_dir, reference_xml_names in anafora.walk(reference_dir):
        try:
            [reference_xml_name] = reference_xml_names
        except ValueError:
            logging.warn("multiple reference files: %s", reference_xml_names)
            reference_xml_name = reference_xml_names[0]
        reference_xml_path = os.path.join(reference_dir, sub_dir, reference_xml_name)

        predicted_xml_paths = glob.glob(os.path.join(predicted_dir, sub_dir, sub_dir + "*.xml"))
        try:
            [predicted_xml_path] = predicted_xml_paths
        except ValueError:
            logging.warn("multiple predicted files: %s", predicted_xml_paths)
            predicted_xml_path = predicted_xml_paths[0]

        reference_data = _load_and_remove_errors(schema, reference_xml_path)
        predicted_data = _load_and_remove_errors(schema, predicted_xml_path)

        named_scores = score_data(reference_data, predicted_data, include, exclude,
                                  annotation_wrapper=annotation_wrapper, file_name=sub_dir)
        for name, scores in named_scores.items():
            result[name].update(scores)

    return result


def score_annotators(schema, anafora_dir, xml_name_regex, include=None, exclude=None, annotation_wrapper=None):
    """
    :param schema: Anafora schema against which Anafora XML should be valdiated
    :param anafora_dir: directory containing Anafora XML directories
    :param xml_name_regex: regular expression matching the annotator files to be compared
    :param include: types of annotations to include (others will be excluded); may be type names,
        (type-name, property-name) tuples, (type-name, property-name, property-value) tuples
    :param set exclude: types of annotations to exclude; may be type names, (type-name, property-name) tuples,
        (type-name, property-name, property-value) tuples
    :param type annotation_wrapper: wrapper object to apply to AnaforaAnnotations
    :return dict: mapping of (annotation type, property) to Scores object
    """
    result = collections.defaultdict(lambda: Scores())

    annotator_name_regex = "([^.]*)[.][^.]*[.]xml$"

    def make_prefix(annotators):
        return "{0}-vs-{1}".format(*sorted(annotators))

    for _, sub_dir, xml_names in anafora.walk(anafora_dir, xml_name_regex):
        if len(xml_names) < 2:
            logging.warn("%s: found fewer than 2 annotators: %s", sub_dir, xml_names)
            continue

        annotator_data = []
        for xml_name in xml_names:
            if '.inprogress.' in xml_name:
                continue
            annotator_name = re.search(annotator_name_regex, xml_name).group(1)
            xml_path = os.path.join(anafora_dir, sub_dir, xml_name)
            if os.stat(xml_path).st_size == 0:
                continue
            data = _load_and_remove_errors(schema, xml_path)
            annotator_data.append((annotator_name, data))

        for i in range(len(annotator_data)):
            annotator1, data1 = annotator_data[i]
            for j in range(i + 1, len(annotator_data)):
                annotator2, data2 = annotator_data[j]
                prefix = make_prefix([annotator1, annotator2])
                general_prefix = make_prefix(
                    a if a == "gold" else "annotator" for a in [annotator1, annotator2])
                named_scores = score_data(data1, data2, include, exclude,
                                          annotation_wrapper=annotation_wrapper, file_name=sub_dir)
                for name, scores in named_scores.items():
                    if not isinstance(name, tuple):
                        name = name,
                    result[(prefix,) + name].update(scores)
                    result[(general_prefix,) + name].update(scores)

    return result


def _print_scores(named_scores):
    """
    :param dict named_scores: mapping of (annotation type, span or property) to Scores object
    """
    def _score_name(name):
        if isinstance(name, tuple):
            name = ":".join(name)
        return name

    print("{0:40}\t{1:^5}\t{2:^5}\t{3:^5}\t{4:^5}\t{5:^5}\t{6:^5}".format(
        "", "ref", "pred", "corr", "P", "R", "F1"))
    for name in sorted(named_scores, key=_score_name):
        scores = named_scores[name]
        print("{0:40}\t{1:5}\t{2:5}\t{3:5}\t{4:5.3f}\t{5:5.3f}\t{6:5.3f}".format(
            _score_name(name), scores.reference, scores.predicted, scores.correct,
            scores.precision(), scores.recall(), scores.f1()))


if __name__ == "__main__":
    def split_tuple_on_colons(string):
        result = tuple(string.split(":"))
        return result[0] if len(result) == 1 else result

    parser = argparse.ArgumentParser()
    parser.add_argument("schema_xml")
    parser.add_argument("reference_dir")
    parser.add_argument("predicted_dir", nargs="?")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--include", nargs="+", type=split_tuple_on_colons)
    parser.add_argument("--exclude", nargs="+", type=split_tuple_on_colons)
    parser.add_argument("--overlap", dest="annotation_wrapper", action="store_const", const=_OverlappingWrapper)
    parser.add_argument("--xml-name-regex", default="[.]xml$")
    args = parser.parse_args()
    basic_config_kwargs = {"format": "%(levelname)s:%(message)s"}
    if args.debug:
        basic_config_kwargs["level"] = logging.DEBUG
    logging.basicConfig(**basic_config_kwargs)

    _schema = anafora.validate.Schema.from_file(args.schema_xml)
    if args.predicted_dir is not None:
        _print_scores(score_dirs(
            _schema, args.reference_dir, args.predicted_dir,
            include=args.include,
            exclude=args.exclude,
            annotation_wrapper=args.annotation_wrapper))
    else:
        _print_scores(score_annotators(
            _schema, args.reference_dir, args.xml_name_regex,
            include=args.include,
            exclude=args.exclude,
            annotation_wrapper=args.annotation_wrapper))
